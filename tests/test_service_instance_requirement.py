"""动作与仪表盘声明「作用于哪台服务实例」的链路测试。

动作与仪表盘挂在插件的分身上，用户配了多份的却是插件提供的服务实例，两个「多」不在
同一根轴上。本文件证的是宿主接手这根轴之后的五件事：不声明该字段的存量声明分毫不变、
形状不合的声明被拒且理由可读、合法声明能拿到候选实例、被引用的实例消失时成因可辨、
以及未选实例时按 §7.2 裁决而不是替用户挑一个。

判据见 docs/plugin-extension-architecture.md §7.2。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.deps import get_current_active_manage_user
from app.api.endpoints import service as service_endpoint
from app.api.endpoints.service import service_instance_selection
from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.declaration import (
    ActionDeclaration,
    DashboardDeclaration,
    ServiceInstanceRequirement,
)
from app.runtime.extensions.admission.action import action_declaration_violation
from app.runtime.extensions.admission.dashboard import (
    dashboard_declaration_violation,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.service_config import (
    configure_service_instance_config_reader,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.admission.service_instance_requirement import (
    REQUIREMENT_FAMILY_ABSENT,
    REQUIREMENT_INSTANCE_ABSENT,
    REQUIREMENT_INSTANCE_DISABLED,
    REQUIREMENT_TYPE_EXCLUDED,
    SERVICE_INSTANCE_PARAM,
    resolve_required_service_instance,
    service_instance_candidates,
    service_instance_reference_issue,
)
from app.schemas.plugin import PluginDashboardMetaItem
from app.schemas.service import ServiceInstanceSelection
from app.schemas.types import ModuleType
from app.schemas.workflow import ActionContext
from app.workflow.actions import invoke_plugin as invoke_plugin_module
from app.workflow.actions.invoke_plugin import InvokePluginAction

DOWNLOADER = ModuleType.Downloader.value
AUTH = ModuleType.Auth.value

# 该族登记表里没有、也不会有的能力标签，用来摆出「族未登记」这一处境
UNREGISTERED = "no-such-family"


def _handler(context, **_kwargs):
    """契约合规的动作实现：接收 context 首个位置参数，回显执行结果。"""
    return True, context


def _conf(name: str, service_type: str = "qbittorrent", *,
          enabled: bool = True, default: bool = False) -> Dict[str, Any]:
    """摆出一条下载器实例配置的原始形状。"""
    return {
        "name": name,
        "type": service_type,
        "enabled": enabled,
        "default": default,
        "config": {"host": "127.0.0.1"},
    }


@pytest.fixture
def instance_configs() -> Iterator[Dict[str, List[Dict[str, Any]]]]:
    """接管服务实例配置的整族读取，用例按能力标签摆出自己需要的配置。

    候选与裁决都从这一条读取端口取数，接管它即可在不碰库的前提下摆出「删了一台」
    「停用一台」这类处境，且用完原样还回先前的 reader。
    """
    store: Dict[str, List[Dict[str, Any]]] = {}
    previous = configure_service_instance_config_reader(
        lambda capability: store.get(capability, [])
    )
    yield store
    configure_service_instance_config_reader(previous)


class _ActionPlugin:
    """声明工作流动作的最小插件桩。"""

    plugin_name = "动作插件"

    def __init__(self, declarations):
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def provides_actions(self):
        """返回声明的工作流动作。"""
        return self._declarations


class _DashboardPlugin:
    """声明仪表盘的最小插件桩。"""

    plugin_name = "仪表盘插件"

    def __init__(self, declarations):
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def provides_dashboards(self):
        """返回声明的仪表盘。"""
        return self._declarations

    def get_dashboard(self, key: str, **kwargs):
        """回显本次取数收到的参数，供断言调用形状。"""
        return {"key": key, **kwargs}, {}, []


class _InvokingPlugin:
    """按声明提供一个动作的插件分身桩，回显本次调用收到的关键字实参。"""

    plugin_name = "作用于服务实例的插件"

    def __init__(self, requirement):
        self._requirement = requirement

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def provides_actions(self):
        """声明唯一的 ping 动作，作用对象由用例给定。"""
        return [ActionDeclaration(
            action_id="ping", name="Ping", impl=self._ping,
            requires_service_instance=self._requirement,
        )]

    def _ping(self, context, **kwargs):
        """把收到的关键字实参写进上下文，供断言调用形状。"""
        context.runtime_state = context.runtime_state or {}
        context.runtime_state["received"] = kwargs
        return True, context


@pytest.fixture
def invoking_plugin(monkeypatch) -> Any:
    """装好一个只跑一个分身的插件，并把工作流动作层指向隔离的插件管理器。

    :return: ``install(requirement) -> plugin``，按给定作用对象装载插件
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    monkeypatch.setattr(invoke_plugin_module, "PluginManager", lambda: manager)

    def install(requirement):
        plugin = _InvokingPlugin(requirement)
        manager.running_plugins["Demo"] = plugin
        return plugin

    yield install
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _RequiringDashboardPlugin(_DashboardPlugin):
    """声明了作用对象的仪表盘插件桩，取数时把收到的关键字实参放进 attrs。"""

    def __init__(self, requirement):
        super().__init__([DashboardDeclaration(
            key="panel", name="面板", requires_service_instance=requirement,
        )])

    def get_render_mode(self):
        """按 vuetify 模式渲染。"""
        return "vuetify", ""

    def get_dashboard(self, key: str, **kwargs):
        """回显本次取数收到的关键字实参，供断言调用形状。"""
        return {"cols": 12}, dict(kwargs), []


@pytest.fixture
def dashboard_manager() -> Any:
    """装好一个声明仪表盘的插件分身，并交出隔离的插件管理器。

    :return: ``install(requirement) -> manager``，按给定作用对象装载插件
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()

    def install(requirement):
        manager.running_plugins["DemoDash"] = _RequiringDashboardPlugin(requirement)
        return manager

    yield install
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _action_descriptors(declarations) -> List[Dict[str, Any]]:
    """把动作声明投影成工作流消费的描述字典列表，全部被拒时为空列表。"""
    projection = PluginProjection({"DemoAction": _ActionPlugin(declarations)})
    groups = projection.actions()
    return groups[0]["actions"] if groups else []


def _dashboard_metadata(declarations) -> List[Dict[str, Any]]:
    """把仪表盘声明投影成元信息条目列表。"""
    projection = PluginProjection({"DemoDash": _DashboardPlugin(declarations)})
    return projection.dashboard_metadata()


# --------------------------------------------------------------------------- #
# 向后兼容：不声明该字段的存量声明分毫不变
# --------------------------------------------------------------------------- #


def test_action_without_requirement_keeps_the_exact_same_descriptor():
    """不声明作用对象的动作，描述字典逐键与该字段存在之前相同。

    新增的键一旦无条件出现，存量前端与存量断言看到的就是另一个形状；键整个不出现
    才谈得上「新增字段不让既有声明失效」。
    """
    descriptors = _action_descriptors(
        [ActionDeclaration(action_id="ping", name="探活", impl=_handler)]
    )

    assert descriptors == [
        {"action_id": "ping", "name": "探活", "func": _handler, "kwargs": {}}
    ]
    assert "requires_service_instance" not in descriptors[0]


def test_dashboard_without_requirement_keeps_the_exact_same_metadata():
    """不声明作用对象的仪表盘，元信息逐键与该字段存在之前相同。"""
    metadata = _dashboard_metadata(
        [DashboardDeclaration(key="panel", name="面板")]
    )

    assert metadata == [{
        "id": "DemoDash",
        "name": "面板",
        "key": "panel",
        "instance_id": "default",
        "instance_key": "DemoDash",
    }]


def test_legacy_bare_dict_declarations_still_register():
    """插件直接交出描述字典的兼容写法不受影响，照常登记与取用。"""
    raw = {"action_id": "ping", "name": "探活", "func": _handler}
    projection = PluginProjection({"DemoAction": _ActionPlugin([raw])})

    assert projection.provided_actions()["DemoAction"] == [raw]
    assert _dashboard_metadata([{"key": "k", "name": "旧面板"}])[0]["key"] == "k"


def test_resolution_is_a_no_op_without_a_requirement():
    """未声明作用对象时裁决整个不参与：既不读配置也不报错，一律答 None。"""
    assert resolve_required_service_instance(None) is None
    assert service_instance_reference_issue(None, "任意名字") is None
    assert service_instance_candidates(None) == ()


# --------------------------------------------------------------------------- #
# 声明期只判形状，理由须可读
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (ServiceInstanceRequirement(capability=""), "非空的能力标签"),
        (ServiceInstanceRequirement(capability="   "), "非空的能力标签"),
        ({"capability": None}, "非空的能力标签"),
        ("downloader", "必须是 ServiceInstanceRequirement"),
        (["downloader"], "必须是 ServiceInstanceRequirement"),
        (ServiceInstanceRequirement(capability="downloader", types=("qb", "")), "非法类型标识"),
        ({"capability": "downloader", "types": "qbittorrent"}, "types 必须是序列"),
    ],
    ids=[
        "capability_empty",
        "capability_blank",
        "capability_none",
        "not_a_requirement",
        "list_instead_of_requirement",
        "types_has_blank_item",
        "types_not_a_sequence",
    ],
)
def test_malformed_requirement_is_rejected_with_a_readable_reason(requirement, expected):
    """形状不合的作用对象声明必须被拒，且拒绝理由说得出是哪里不合。

    理由在契约校验处断言而不在日志里：日志的措辞与去向都可能变，而校验函数交出的
    那句话就是用户最终看到的那句话。
    """
    declaration = ActionDeclaration(
        action_id="ping", name="探活", impl=_handler,
        requires_service_instance=requirement,
    )

    violation = action_declaration_violation(declaration)

    assert violation is not None and expected in violation
    assert _action_descriptors([declaration]) == []


def test_dashboard_requirement_shape_is_validated_too():
    """仪表盘声明的作用对象同样只判形状，不合形状即整条被拒。

    整条被拒之后该插件退化为「一块默认仪表盘」，与它从未声明过 provides_dashboards
    时的行为一致——被拒的是这条声明，不是这个插件。
    """
    declaration = DashboardDeclaration(
        key="panel", name="面板",
        requires_service_instance=ServiceInstanceRequirement(capability=""),
    )

    violation = dashboard_declaration_violation(declaration)

    assert violation is not None and "非空的能力标签" in violation
    assert [item["key"] for item in _dashboard_metadata([declaration])] == [""]


def test_unregistered_capability_is_accepted_at_declaration_time():
    """尚未登记的族照样收下：声明校验每次投影都重跑，按登记状态判会让同一条声明忽合忽不合。

    带进新族的扩展晚一步登记，先前那条拒绝理由自己就会消失——声明是否成立必须只取决于
    声明自身写了什么。存在性改由取用期回答。
    """
    descriptors = _action_descriptors([
        ActionDeclaration(
            action_id="ping", name="探活", impl=_handler,
            requires_service_instance=ServiceInstanceRequirement(capability=UNREGISTERED),
        )
    ])

    assert descriptors[0]["requires_service_instance"] == {
        "capability": UNREGISTERED, "types": []
    }
    assert not service_family_registry.is_registered(UNREGISTERED)


def test_one_bad_declaration_skips_only_itself():
    """一条坏声明只跳过它自己，同一插件的其余声明照常登记。"""
    descriptors = _action_descriptors([
        ActionDeclaration(action_id="ok1", name="好的一", impl=_handler),
        ActionDeclaration(
            action_id="bad", name="坏的", impl=_handler,
            requires_service_instance=ServiceInstanceRequirement(capability=""),
        ),
        ActionDeclaration(
            action_id="ok2", name="好的二", impl=_handler,
            requires_service_instance=ServiceInstanceRequirement(capability=DOWNLOADER),
        ),
    ])

    assert [item["action_id"] for item in descriptors] == ["ok1", "ok2"]


def test_one_bad_dashboard_declaration_skips_only_itself():
    """仪表盘同理：一条坏声明不牵连同一插件的其余仪表盘。"""
    metadata = _dashboard_metadata([
        DashboardDeclaration(key="good", name="好面板"),
        DashboardDeclaration(
            key="bad", name="坏面板",
            requires_service_instance={"capability": "downloader", "types": 3},
        ),
    ])

    assert [item["key"] for item in metadata] == ["good"]


# --------------------------------------------------------------------------- #
# 取用期：候选实例与失效成因
# --------------------------------------------------------------------------- #


def test_candidates_come_from_the_family_config_list(instance_configs):
    """声明了合法能力标签时，候选实例取自该族的配置列表，按类型与实例名升序。"""
    instance_configs[DOWNLOADER] = [
        _conf("乙下载器"), _conf("甲下载器"), _conf("传输", "transmission"),
    ]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    names = [item.name for item in service_instance_candidates(requirement)]

    assert names == ["乙下载器", "甲下载器", "传输"]


def test_candidates_list_disabled_instances_and_mark_them(instance_configs):
    """停用的实例照样列出并标注启用态：藏起来会让用户以为配置丢了。"""
    instance_configs[DOWNLOADER] = [_conf("在用"), _conf("停用的", enabled=False)]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    listed = {item.name: item.enabled for item in service_instance_candidates(requirement)}

    assert listed == {"在用": True, "停用的": False}


def test_types_narrowing_excludes_other_types(instance_configs):
    """声明收窄了类型时，别的类型的实例不进候选。"""
    instance_configs[DOWNLOADER] = [_conf("qb 的"), _conf("tr 的", "transmission")]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER, types=("qbittorrent",))

    names = [item.name for item in service_instance_candidates(requirement)]

    assert names == ["qb 的"]


@pytest.mark.parametrize(
    ("capability", "types", "selected", "expected"),
    [
        (UNREGISTERED, (), "任意", REQUIREMENT_FAMILY_ABSENT),
        (DOWNLOADER, (), "已经删掉的", REQUIREMENT_INSTANCE_ABSENT),
        (DOWNLOADER, (), "停用的", REQUIREMENT_INSTANCE_DISABLED),
        (DOWNLOADER, ("qbittorrent",), "tr 的", REQUIREMENT_TYPE_EXCLUDED),
        (DOWNLOADER, (), "在用", None),
    ],
    ids=["family_absent", "instance_absent", "instance_disabled", "type_excluded", "still_valid"],
)
def test_reference_issue_separates_the_four_causes(
    instance_configs, capability, types, selected, expected
):
    """被引用的实例消失时提示可辨：四种成因分开，处置动作各不相同。

    装回带来该族的扩展、重建配置、启用配置、改选一台类型对得上的——笼统答一个
    「不可用」会让这四种处境看起来一样。
    """
    instance_configs[DOWNLOADER] = [
        _conf("在用"), _conf("停用的", enabled=False), _conf("tr 的", "transmission"),
    ]
    requirement = ServiceInstanceRequirement(capability=capability, types=types)

    assert service_instance_reference_issue(requirement, selected) == expected


def test_resolution_of_a_vanished_instance_names_it_and_lists_candidates(instance_configs):
    """引用了已消失的实例时报错，报错里说出是哪一个、并列出当前可选的实例。"""
    instance_configs[DOWNLOADER] = [_conf("在用"), _conf("停用的", enabled=False)]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    with pytest.raises(LookupError) as error:
        resolve_required_service_instance(requirement, "已经删掉的")

    message = str(error.value)
    assert "已经删掉的" in message
    assert "在用（已启用）" in message
    assert "停用的（已停用）" in message


# --------------------------------------------------------------------------- #
# §7.2：未选实例时的裁决，绝不取第一个
# --------------------------------------------------------------------------- #


def test_unselected_uses_the_explicit_default_target(instance_configs):
    """未选实例且有已启用的显式默认调用目标时，用那一个。"""
    instance_configs[DOWNLOADER] = [_conf("首位"), _conf("默认的", default=True)]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    assert resolve_required_service_instance(requirement) == "默认的"


def test_unselected_without_default_refuses_instead_of_taking_the_first(instance_configs):
    """未选实例且没有默认调用目标时报错并列出候选，绝不取第一个。"""
    instance_configs[DOWNLOADER] = [_conf("甲"), _conf("乙")]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    with pytest.raises(LookupError) as error:
        resolve_required_service_instance(requirement)

    message = str(error.value)
    assert "甲（已启用）" in message and "乙（已启用）" in message
    assert "未设置默认调用目标" in message


def test_unselected_with_disabled_default_refuses_instead_of_falling_over(instance_configs):
    """默认调用目标已停用等同于没有默认，报错而不是改走另一个启用实例。"""
    instance_configs[DOWNLOADER] = [_conf("在用"), _conf("默认的", default=True, enabled=False)]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    with pytest.raises(LookupError) as error:
        resolve_required_service_instance(requirement)

    assert "默认的 已停用" in str(error.value)


def test_single_candidate_is_still_not_picked_without_a_default(instance_configs):
    """只有一个候选也不替用户挑：没有默认就是没有默认，独苗同样要显式指定。"""
    instance_configs[DOWNLOADER] = [_conf("独苗")]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    with pytest.raises(LookupError):
        resolve_required_service_instance(requirement)


def test_family_without_default_target_always_requires_an_explicit_choice(instance_configs):
    """没有默认调用目标这个概念的族（登录认证），未选实例一律报错。"""
    instance_configs[AUTH] = [{"name": "入口", "type": "sso", "enabled": True, "config": {}}]
    requirement = ServiceInstanceRequirement(capability=AUTH)

    with pytest.raises(LookupError) as error:
        resolve_required_service_instance(requirement)

    assert "没有默认调用目标" in str(error.value)


def test_explicit_selection_wins_over_the_default(instance_configs):
    """显式选中的实例优先于默认调用目标：用户的选择不该被默认覆盖。"""
    instance_configs[DOWNLOADER] = [_conf("选中的"), _conf("默认的", default=True)]
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER)

    assert resolve_required_service_instance(requirement, "选中的") == "选中的"


# --------------------------------------------------------------------------- #
# 调用形状：只对声明了作用对象的扩展点变化
# --------------------------------------------------------------------------- #


def test_resolved_instance_reaches_the_action_impl(instance_configs, invoking_plugin):
    """未选实例时按族的默认调用目标裁决，实例名经完整调用链交回动作实现。"""
    instance_configs[DOWNLOADER] = [_conf("首位"), _conf("默认的", default=True)]
    plugin = invoking_plugin(
        ServiceInstanceRequirement(capability=DOWNLOADER)
    )

    context = InvokePluginAction("node-1").execute(
        workflow_id=1,
        params={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
        context=ActionContext(),
    )

    assert context.runtime_state["received"] == {SERVICE_INSTANCE_PARAM: "默认的"}
    assert plugin.plugin_name == "作用于服务实例的插件"


def test_user_selection_reaches_the_action_impl(instance_configs, invoking_plugin):
    """用户在工作流节点上选中的实例名原样交回实现，不被默认调用目标顶掉。"""
    instance_configs[DOWNLOADER] = [_conf("选中的"), _conf("默认的", default=True)]
    invoking_plugin(ServiceInstanceRequirement(capability=DOWNLOADER))

    context = InvokePluginAction("node-1").execute(
        workflow_id=1,
        params={
            "plugin_id": "Demo", "action_id": "ping",
            "action_params": {SERVICE_INSTANCE_PARAM: "选中的"},
        },
        context=ActionContext(),
    )

    assert context.runtime_state["received"] == {SERVICE_INSTANCE_PARAM: "选中的"}


def test_action_without_requirement_gets_no_extra_keyword(instance_configs, invoking_plugin):
    """未声明作用对象的动作，参数原样展开，调用里不会多出这个关键字。"""
    instance_configs[DOWNLOADER] = [_conf("主力", default=True)]
    invoking_plugin(None)

    context = InvokePluginAction("node-1").execute(
        workflow_id=1,
        params={"plugin_id": "Demo", "action_id": "ping", "action_params": {"foo": 1}},
        context=ActionContext(),
    )

    assert context.runtime_state["received"] == {"foo": 1}


def test_invoke_raises_with_candidates_when_the_selection_vanished(
    instance_configs, invoking_plugin
):
    """引用的实例消失时 LookupError 冒出工作流引擎，消息里列出当前候选。

    工作流引擎把异常转成用户可见的失败原因；吞掉后返回未变化的 context，用户看到的
    是一次「成功」但什么都没发生。
    """
    instance_configs[DOWNLOADER] = [_conf("在用")]
    invoking_plugin(ServiceInstanceRequirement(capability=DOWNLOADER))

    with pytest.raises(LookupError) as error:
        InvokePluginAction("node-1").execute(
            workflow_id=1,
            params={
                "plugin_id": "Demo", "action_id": "ping",
                "action_params": {SERVICE_INSTANCE_PARAM: "已经删掉的"},
            },
            context=ActionContext(),
        )

    assert "已经删掉的" in str(error.value)
    assert "在用（已启用）" in str(error.value)


def test_invoke_refuses_instead_of_picking_the_first_when_unselected(
    instance_configs, invoking_plugin
):
    """声明了能力却没选实例、该族又没有默认调用目标时报错，绝不替用户挑第一个。"""
    instance_configs[DOWNLOADER] = [_conf("甲"), _conf("乙")]
    invoking_plugin(ServiceInstanceRequirement(capability=DOWNLOADER))

    with pytest.raises(LookupError) as error:
        InvokePluginAction("node-1").execute(
            workflow_id=1,
            params={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
            context=ActionContext(),
        )

    assert "甲（已启用）" in str(error.value) and "乙（已启用）" in str(error.value)


def test_resolved_instance_reaches_the_dashboard_fetch(instance_configs, dashboard_manager):
    """声明了作用对象的仪表盘，取数时收到解析出的实例名。"""
    instance_configs[DOWNLOADER] = [_conf("主力", default=True)]
    manager = dashboard_manager(
        ServiceInstanceRequirement(capability=DOWNLOADER)
    )

    dashboard = manager.get_plugin_dashboard("DemoDash", "panel")

    assert dashboard.attrs[SERVICE_INSTANCE_PARAM] == "主力"


def test_dashboard_without_requirement_receives_no_extra_keyword(
    instance_configs, dashboard_manager
):
    """未声明作用对象的仪表盘，取数形状与该字段存在之前一字不改。"""
    instance_configs[DOWNLOADER] = [_conf("主力", default=True)]
    manager = dashboard_manager(None)

    dashboard = manager.get_plugin_dashboard("DemoDash", "panel")

    assert SERVICE_INSTANCE_PARAM not in dashboard.attrs


def test_dashboard_fetch_never_drags_in_the_declaration_face_for_legacy_plugins():
    """没声明 provides_dashboards 的插件取数不触达声明面。

    取数在本字段存在之前从不读声明；为了读一个它根本没有的字段而把整条取数链路绑上
    投影，等于让旧写法的仪表盘随投影一起失败。
    """
    class _LegacyDashboardPlugin:
        """只实现 get_dashboard 的旧写法插件，连 get_state 都没有。"""

        plugin_name = "旧仪表盘插件"

        def get_render_mode(self):
            """按 vuetify 模式渲染。"""
            return "vuetify", ""

        def get_dashboard(self, key: str, **kwargs):
            """交出一块固定的仪表盘。"""
            return {"cols": 12}, {}, []

    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    try:
        manager.running_plugins["Legacy"] = _LegacyDashboardPlugin()

        dashboard = manager.get_plugin_dashboard("Legacy", "")

        assert dashboard.cols == {"cols": 12}
    finally:
        Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_dashboard_fetch_refuses_instead_of_picking_the_first(
    instance_configs, dashboard_manager
):
    """仪表盘声明了能力却选不出实例时以 409 退回并列出候选，绝不取第一个。"""
    instance_configs[DOWNLOADER] = [_conf("甲"), _conf("乙")]
    manager = dashboard_manager(ServiceInstanceRequirement(capability=DOWNLOADER))

    with pytest.raises(HTTPException) as error:
        manager.get_plugin_dashboard("DemoDash", "panel")

    assert error.value.status_code == 409
    assert "甲（已启用）" in error.value.detail and "乙（已启用）" in error.value.detail


def test_dashboard_metadata_carries_the_requirement():
    """声明了作用对象的仪表盘，元信息里带上该族坐标供前端渲染选择器。"""
    metadata = _dashboard_metadata([
        DashboardDeclaration(
            key="panel", name="面板",
            requires_service_instance=ServiceInstanceRequirement(
                capability=DOWNLOADER, types=("qbittorrent",)
            ),
        )
    ])

    assert metadata[0]["requires_service_instance"] == {
        "capability": DOWNLOADER, "types": ["qbittorrent"]
    }


# --------------------------------------------------------------------------- #
# 实例选择器端点
# --------------------------------------------------------------------------- #


def test_selection_endpoint_hands_out_the_candidate_list(instance_configs):
    """声明了合法能力标签时，端点交得出可选实例列表。"""
    instance_configs[DOWNLOADER] = [_conf("主力", default=True), _conf("备用", enabled=False)]

    payload = service_instance_selection(DOWNLOADER, [], "", None)

    assert payload["family_registered"] is True
    assert payload["supports_default_target"] is True
    assert payload["candidates"] == [
        {"type": "qbittorrent", "name": "主力", "enabled": True, "is_default_target": True},
        {"type": "qbittorrent", "name": "备用", "enabled": False, "is_default_target": False},
    ]
    assert payload["issue"] is None


def test_selection_endpoint_separates_unregistered_family_from_empty_list(instance_configs):
    """族未登记与族登记了却没有配置分开回答：一个装回扩展，一个去设置页新建。"""
    instance_configs[DOWNLOADER] = []

    absent = service_instance_selection(UNREGISTERED, [], "", None)
    empty = service_instance_selection(DOWNLOADER, [], "", None)

    assert absent["family_registered"] is False and absent["candidates"] == []
    assert empty["family_registered"] is True and empty["candidates"] == []


def test_selection_endpoint_reports_the_vanished_selection(instance_configs):
    """端点一并判定已选实例的现状，交出稳定的成因代码而不是文案。"""
    instance_configs[DOWNLOADER] = [_conf("在用")]

    payload = service_instance_selection(DOWNLOADER, [], "已经删掉的", None)

    assert payload["selected"] == "已经删掉的"
    assert payload["issue"] == REQUIREMENT_INSTANCE_ABSENT


def test_selection_endpoint_never_leaks_the_config_payload(instance_configs):
    """候选只带身份与启用态：配置载荷里装着凭据，随选择器下发即等于摊给所有人。"""
    instance_configs[DOWNLOADER] = [{
        "name": "主力", "type": "qbittorrent", "enabled": True,
        "config": {"host": "h", "password": "s3cret"},
    }]

    payload = service_instance_selection(DOWNLOADER, [], "", None)

    assert set(payload["candidates"][0]) == {"type", "name", "enabled", "is_default_target"}


# --------------------------------------------------------------------------- #
# response_model 不得静默裁掉新增字段
# --------------------------------------------------------------------------- #


def test_dashboard_meta_requirement_survives_the_response_model():
    """仪表盘元信息里的嵌套作用对象必须原样穿过 ``response_model``。

    ``response_model`` 的校验/序列化等价于对返回字典执行一次 ``Model(**payload).model_dump()``；
    模型上没有这个字段，嵌套结构就在这一步被静默裁掉，端点看起来正常却少一层。
    """
    payload = _dashboard_metadata([
        DashboardDeclaration(
            key="panel", name="面板",
            requires_service_instance=ServiceInstanceRequirement(
                capability=DOWNLOADER, types=("qbittorrent",)
            ),
        )
    ])[0]

    serialized = PluginDashboardMetaItem(**payload).model_dump()

    assert serialized["requires_service_instance"] == {
        "capability": DOWNLOADER, "types": ["qbittorrent"]
    }


def test_selection_survives_the_real_response_model_serialization(instance_configs):
    """走真实 ASGI 链路取选择器，嵌套候选列表必须原样到达客户端且不带凭据。

    直接调用端点函数绕过了 FastAPI 的 ``response_model`` 校验与序列化，而字段正是在
    那一步被静默裁掉的。
    """
    instance_configs[DOWNLOADER] = [{
        "name": "主力", "type": "qbittorrent", "enabled": True, "default": True,
        "config": {"host": "h", "password": "s3cret"},
    }]

    async def _fetch() -> httpx.Response:
        """经 ASGI 传输取一次该族的可选实例。"""
        app = FastAPI()
        app.include_router(service_endpoint.router, prefix="/service")
        app.dependency_overrides[get_current_active_manage_user] = lambda: SimpleNamespace(
            id=1, name="manager", is_superuser=False
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(
                f"/service/instance_candidates/{DOWNLOADER}",
                params={"types": "qbittorrent", "selected": "主力"},
            )

    response = asyncio.run(_fetch())

    assert response.status_code == 200
    assert "s3cret" not in response.text
    body = response.json()["data"]
    assert set(ServiceInstanceSelection(**body).model_dump()) == set(body)
    assert body["candidates"] == [
        {"type": "qbittorrent", "name": "主力", "enabled": True, "is_default_target": True}
    ]
    assert body["issue"] is None


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
@pytest.mark.parametrize(
    ("configs", "selected"),
    [
        ([_conf("在用")], "已经删掉的"),
        ([_conf("停用的", enabled=False)], "停用的"),
        ([_conf("tr 的", "transmission")], "tr 的"),
        ([_conf("甲"), _conf("乙")], None),
        ([_conf("默认的", default=True, enabled=False)], None),
    ],
    ids=["absent", "disabled", "excluded", "no_default", "disabled_default"],
)
def test_every_raised_message_is_registered_in_both_locales(
    instance_configs, locale, configs, selected
):
    """每一句用户看得见的报错都要在两份语言包里登记，否则原样漏出中文。

    句子按整句登记模式：拼出来的句子在任何一份译文里都找不到对应项，因此报错文案不能
    由「成因片段加候选片段」拼成。

    译文里必须还带着候选列表：既有那条更短的「没有默认调用目标」模式与本轮几句只差一个
    后缀，登记漏了就会退回原文，登记对了也要确认赢的是完整那条而不是短的那条把后半句
    吃掉——匹配走 fullmatch，这条断言把它钉住。
    """
    from app.runtime.localization import LocaleHelper

    instance_configs[DOWNLOADER] = configs
    requirement = ServiceInstanceRequirement(capability=DOWNLOADER, types=("qbittorrent",))

    with pytest.raises(LookupError) as error:
        resolve_required_service_instance(requirement, selected)

    message = str(error.value)
    translated = LocaleHelper.translate_text(message, locale)

    assert translated != message
    assert message.rsplit("：", 1)[-1] in translated


def test_action_descriptor_requirement_survives_the_response_model():
    """动作组下发的嵌套作用对象必须原样穿过 ``response_model``。"""
    from app.schemas.workflow import PluginWorkflowActionGroup

    descriptors = _action_descriptors([
        ActionDeclaration(
            action_id="ping", name="探活", impl=_handler,
            requires_service_instance=ServiceInstanceRequirement(
                capability=DOWNLOADER, types=("qbittorrent",)
            ),
        )
    ])
    group = {
        "plugin_id": "DemoAction",
        "plugin_name": "动作插件",
        "actions": [
            {key: value for key, value in descriptors[0].items() if key != "func"}
        ],
    }

    serialized = PluginWorkflowActionGroup(**group).model_dump()

    assert serialized["actions"][0]["requires_service_instance"] == {
        "capability": DOWNLOADER, "types": ["qbittorrent"]
    }
