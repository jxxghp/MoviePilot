"""筛选规则来源端点：三层可辨、冲突可见、列举确定与响应模型穿透。

运行期规则集是内置 < 插件 < 用户三层合并出的一张平表，合并完就看不出哪条来自哪里，
插件带来的规则因此在设置页上无从辨认；跨插件同名而整体失效的标识更是只在日志里留过
一次告警。本文件锁住这两件事在端点上说得清楚。
"""

import inspect
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import patch

import pytest

from app.api.deps import get_current_active_superuser
from app.api.endpoints import filterrule as filterrule_endpoint
from app.application.rules import (
    BUILTIN_LAYER,
    PLUGIN_LAYER,
    USER_LAYER,
    FilterRuleOriginService,
)
from app.domain.filterrule import BUILTIN_RULE_SET
from app.runtime.extensions.registry.filter_rule import (
    RULE_GROUP_KIND,
    RULE_KIND,
    PluginFilterRuleRegistry,
)
from app.schemas.rule import FilterRuleOrigin

# 取一个真实存在的内置规则标识，用于验证插件与用户如何压住内置层
_BUILTIN_RULE_ID = "BLU"


class _RuleHelper:
    """用户自定义规则的轻量替身，避免依赖真实系统配置。"""

    def __init__(self, rules: List[Any] = None) -> None:
        """记录用户自定义规则。

        :param rules: 自定义规则对象列表
        """
        self._rules = rules or []

    def get_custom_rules(self) -> List[Any]:
        """返回用户配置的全部自定义过滤规则。

        :return: 自定义规则对象列表
        """
        return self._rules


def _custom(rule_id: str, include: str = "user") -> Any:
    """构造一条用户自定义规则。

    :param rule_id: 规则标识
    :param include: 包含条件
    :return: 具备 id 与 model_dump 的自定义规则替身
    """
    payload = {"id": rule_id, "name": rule_id, "include": include}
    return SimpleNamespace(id=rule_id, model_dump=lambda: dict(payload))


@pytest.fixture
def registry() -> PluginFilterRuleRegistry:
    """构造隔离的插件筛选规则注册表，告警不落到真实日志。"""
    return PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))


@pytest.fixture
def user_groups() -> Iterator[List[dict]]:
    """接管用户规则组配置读取，用例改列表内容即改配置。"""
    groups: List[dict] = []
    config = SimpleNamespace(get=lambda _key: groups)
    with patch(
        "app.application.rules.get_configured_system_config", return_value=config
    ):
        yield groups


def _service(registry: PluginFilterRuleRegistry, rules: List[Any] = None):
    """构造绑定测试替身的来源服务。

    :param registry: 插件筛选规则注册表
    :param rules: 用户自定义规则
    :return: 来源应用服务
    """
    return FilterRuleOriginService(registry=registry, rule_helper=_RuleHelper(rules))


def _origin(origins: List[FilterRuleOrigin], identity: str) -> FilterRuleOrigin:
    """按标识取出一条来源条目。

    :param origins: 来源条目列表
    :param identity: 规则标识或规则组名
    :return: 该标识的来源条目
    """
    return next(origin for origin in origins if origin.id == identity)


def test_builtin_rules_report_the_builtin_layer(registry, user_groups):
    """未被任何插件或用户覆盖的规则来自内置层。"""
    origins = _service(registry).list_rule_origins()

    entry = _origin(origins, _BUILTIN_RULE_ID)
    assert entry.effective is True
    assert entry.source.layer == BUILTIN_LAYER
    assert entry.source.owner is None
    assert entry.shadowed == []
    assert entry.definition == BUILTIN_RULE_SET[_BUILTIN_RULE_ID]


def test_plugin_rules_report_which_plugin_instance_they_came_from(registry, user_groups):
    """插件带来的规则要指出是哪个插件的哪个分身，用户才知道该去停用谁。"""
    registry.register("AcmePlugin@alt", rules=[("ACMEWEB", {"include": "Acme"})])

    entry = _origin(_service(registry).list_rule_origins(), "ACMEWEB")

    assert entry.effective is True
    assert entry.source.layer == PLUGIN_LAYER
    assert entry.source.owner == "AcmePlugin@alt"
    assert entry.source.extension_id == "AcmePlugin"
    assert entry.source.instance_id == "alt"
    assert entry.definition == {"include": "Acme"}


def test_user_rules_win_over_plugin_and_builtin(registry, user_groups):
    """用户自定义永远赢，被压住的下层一并交出。"""
    registry.register("AcmePlugin", rules=[(_BUILTIN_RULE_ID, {"include": "plugin"})])

    entry = _origin(
        _service(registry, [_custom(_BUILTIN_RULE_ID)]).list_rule_origins(),
        _BUILTIN_RULE_ID,
    )

    assert entry.source.layer == USER_LAYER
    assert [layer.layer for layer in entry.shadowed] == [BUILTIN_LAYER, PLUGIN_LAYER]
    assert entry.shadowed[1].owner == "AcmePlugin"
    assert entry.definition["include"] == "user"


def test_plugin_rule_shadows_the_builtin_definition(registry, user_groups):
    """插件覆盖内置标识时，生效的是插件那一份，内置退为被压住的下层。"""
    registry.register("AcmePlugin", rules=[(_BUILTIN_RULE_ID, {"include": "plugin"})])

    entry = _origin(_service(registry).list_rule_origins(), _BUILTIN_RULE_ID)

    assert entry.source.layer == PLUGIN_LAYER
    assert [layer.layer for layer in entry.shadowed] == [BUILTIN_LAYER]
    assert entry.definition == {"include": "plugin"}


def test_three_layers_are_distinguishable_in_one_listing(registry, user_groups):
    """一次列举里三层来源各自可辨。"""
    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])

    origins = _service(registry, [_custom("USERONLY")]).list_rule_origins()

    assert _origin(origins, _BUILTIN_RULE_ID).source.layer == BUILTIN_LAYER
    assert _origin(origins, "ACMEWEB").source.layer == PLUGIN_LAYER
    assert _origin(origins, "USERONLY").source.layer == USER_LAYER


def test_conflicted_rule_is_visible_with_the_plugins_involved(registry, user_groups):
    """因跨插件同名而失效的标识仍要交出，并说清涉及哪些插件。"""
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin@two", rules=[("SHARED", {"include": "other"})])

    entry = _origin(_service(registry).list_rule_origins(), "SHARED")

    assert entry.effective is False
    assert entry.source is None
    assert entry.definition is None
    assert entry.conflict is not None
    assert entry.conflict.plugins == ["AcmePlugin", "OtherPlugin"]
    assert entry.conflict.owners == ["AcmePlugin", "OtherPlugin@two"]


def test_conflicted_builtin_identity_falls_back_to_the_builtin_definition(
    registry, user_groups
):
    """冲突的若是内建标识，插件声明全部作废后该标识回落为内建定义。"""
    registry.register("AcmePlugin", rules=[(_BUILTIN_RULE_ID, {"include": "acme"})])
    registry.register("OtherPlugin", rules=[(_BUILTIN_RULE_ID, {"include": "other"})])

    entry = _origin(_service(registry).list_rule_origins(), _BUILTIN_RULE_ID)

    assert entry.effective is True
    assert entry.source.layer == BUILTIN_LAYER
    assert entry.definition == BUILTIN_RULE_SET[_BUILTIN_RULE_ID]
    assert entry.conflict.plugins == ["AcmePlugin", "OtherPlugin"]


def test_conflict_endpoint_lists_only_conflicted_identities(registry, user_groups):
    """冲突端点只交出存在冲突的标识，规则与规则组一并覆盖。"""
    registry.register(
        "AcmePlugin",
        rules=[("SHARED", {"include": "acme"}), ("ACMEONLY", {"include": "a"})],
        groups=[("共享组", {"rule_string": "A"})],
    )
    registry.register(
        "OtherPlugin",
        rules=[("SHARED", {"include": "other"})],
        groups=[("共享组", {"rule_string": "B"})],
    )

    conflicts = _service(registry).list_conflicts()

    assert [(entry.kind, entry.id) for entry in conflicts] == [
        (RULE_KIND, "SHARED"), (RULE_GROUP_KIND, "共享组")
    ]
    assert all(entry.effective is False for entry in conflicts)


def test_plugin_rule_groups_report_their_owner(registry, user_groups):
    """插件提供的规则组同样指出归属的插件实例。"""
    registry.register("AcmePlugin@alt", groups=[("Acme 组", {"rule_string": "BLU"})])

    entry = _origin(_service(registry).list_rule_group_origins(), "Acme 组")

    assert entry.kind == RULE_GROUP_KIND
    assert entry.source.layer == PLUGIN_LAYER
    assert entry.source.owner == "AcmePlugin@alt"
    assert entry.definition == {"rule_string": "BLU"}


def test_user_rule_group_wins_over_the_plugin_one(registry, user_groups):
    """同名规则组以用户配置为准，插件那一份退为被压住的下层。"""
    registry.register("AcmePlugin", groups=[("共享组", {"rule_string": "PLUGIN"})])
    user_groups.append({"name": "共享组", "rule_string": "USER"})

    entry = _origin(_service(registry).list_rule_group_origins(), "共享组")

    assert entry.source.layer == USER_LAYER
    assert [layer.layer for layer in entry.shadowed] == [PLUGIN_LAYER]
    assert entry.definition["rule_string"] == "USER"


def test_listing_order_is_independent_of_registration_order(registry, user_groups):
    """列举顺序按标识排序，与插件登记先后无关。"""
    forward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    forward.register("ZebraPlugin", rules=[("ZZZRULE", {"include": "z"})])
    forward.register("AcmePlugin", rules=[("AAARULE", {"include": "a"})])

    backward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    backward.register("AcmePlugin", rules=[("AAARULE", {"include": "a"})])
    backward.register("ZebraPlugin", rules=[("ZZZRULE", {"include": "z"})])

    forward_ids = [entry.id for entry in _service(forward).list_rule_origins()]
    backward_ids = [entry.id for entry in _service(backward).list_rule_origins()]

    assert forward_ids == backward_ids == sorted(forward_ids)


def test_conflict_plugin_list_is_independent_of_registration_order(registry, user_groups):
    """冲突涉及的插件清单已排序，与登记先后无关。"""
    forward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    forward.register("ZebraPlugin", rules=[("SHARED", {"include": "z"})])
    forward.register("AcmePlugin", rules=[("SHARED", {"include": "a"})])

    backward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    backward.register("AcmePlugin", rules=[("SHARED", {"include": "a"})])
    backward.register("ZebraPlugin", rules=[("SHARED", {"include": "z"})])

    assert (
        _origin(_service(forward).list_rule_origins(), "SHARED").conflict.plugins
        == _origin(_service(backward).list_rule_origins(), "SHARED").conflict.plugins
        == ["AcmePlugin", "ZebraPlugin"]
    )


def test_stopping_a_plugin_removes_its_rules_from_the_listing(registry, user_groups):
    """插件停用后其规则不再出现在来源列表里。"""
    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])
    assert any(
        entry.id == "ACMEWEB" for entry in _service(registry).list_rule_origins()
    )

    registry.unregister_owner("AcmePlugin")

    assert all(
        entry.id != "ACMEWEB" for entry in _service(registry).list_rule_origins()
    )


def test_response_model_keeps_every_nested_field_the_endpoint_returns(
    registry, user_groups
):
    """端点返回的嵌套字段必须全部能穿过响应模型，否则会被 FastAPI 静默裁掉。"""
    registry.register(
        "AcmePlugin@alt",
        rules=[(_BUILTIN_RULE_ID, {"include": ["a"], "exclude": ["b"]})],
    )

    payload = _service(registry).list_rule_origins()
    entry = _origin(payload, _BUILTIN_RULE_ID)
    serialized = FilterRuleOrigin(**entry.model_dump()).model_dump()

    assert set(serialized) == set(entry.model_dump())
    assert serialized["source"]["owner"] == "AcmePlugin@alt"
    assert serialized["source"]["extension_id"] == "AcmePlugin"
    assert serialized["source"]["instance_id"] == "alt"
    assert serialized["shadowed"][0]["layer"] == BUILTIN_LAYER
    assert serialized["definition"] == {"include": ["a"], "exclude": ["b"]}


def test_response_model_keeps_the_conflict_plugin_list(registry, user_groups):
    """冲突详情是嵌套结构，同样要能整体穿过响应模型。"""
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])

    entry = _origin(_service(registry).list_rule_origins(), "SHARED")
    serialized = FilterRuleOrigin(**entry.model_dump()).model_dump()

    assert serialized["conflict"]["plugins"] == ["AcmePlugin", "OtherPlugin"]
    assert serialized["conflict"]["owners"] == ["AcmePlugin", "OtherPlugin"]
    assert serialized["effective"] is False


def test_registry_diagnose_still_reports_each_claiming_owner(registry):
    """诊断信息仍按声明方逐条给出，冲突双方都在其中。"""
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])

    entries = registry.diagnose()

    assert entries == [
        {"kind": RULE_KIND, "identity": "SHARED", "owner": "AcmePlugin", "effective": False},
        {"kind": RULE_KIND, "identity": "SHARED", "owner": "OtherPlugin", "effective": False},
    ]


def test_filter_rule_endpoints_require_superuser():
    """规则来源暴露的是全局筛选行为，按设置类端点的口径限管理员。"""
    def dependency(func: Any) -> Any:
        """读取端点参数上声明的依赖函数。"""
        return inspect.signature(func).parameters["_"].default.dependency

    assert dependency(filterrule_endpoint.rule_origins) is get_current_active_superuser
    assert dependency(filterrule_endpoint.rule_group_origins) is get_current_active_superuser
    assert dependency(filterrule_endpoint.rule_conflicts) is get_current_active_superuser


def test_endpoints_delegate_to_the_origin_service(registry, user_groups):
    """端点只做转发，取数走应用层服务而不是自己够进注册表。"""
    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])
    service = _service(registry)

    with patch(
        "app.api.endpoints.filterrule.FilterRuleOriginService", return_value=service
    ):
        rules = filterrule_endpoint.rule_origins(None)
        groups = filterrule_endpoint.rule_group_origins(None)
        conflicts = filterrule_endpoint.rule_conflicts(None)

    assert _origin(rules, "ACMEWEB").source.layer == PLUGIN_LAYER
    assert groups == []
    assert conflicts == []
