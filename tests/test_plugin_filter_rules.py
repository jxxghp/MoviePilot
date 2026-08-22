"""插件声明筛选规则链路测试：契约校验、三级优先级、跨插件冲突、停用回收与 Rust 快路。"""

from types import SimpleNamespace
from typing import Iterator, List, Optional

import pytest

from app.domain.context import MediaInfo, TorrentInfo
from app.domain.filterrule import BUILTIN_RULE_SET, RuleParser
from app.foundation.singleton import Singleton
from app.modules.filter import FilterModule
from app.runtime.extensions.contract.declaration import (
    FilterRuleDeclaration,
    FilterRuleGroupDeclaration,
)
from app.runtime.extensions.registry.filter_rule import (
    PluginFilterRuleRegistry,
    plugin_filter_rule_registry,
)
from app.runtime.extensions.admission.filter_rule import (
    filter_rule_declaration_violation,
    filter_rule_group_declaration_violation,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.hostports.torrentanalysis import torrent_analysis_port
from app.schemas.rule import is_valid_rule_id, rule_string_violation


@pytest.fixture(autouse=True)
def _isolate_filter_rule_registry() -> Iterator[None]:
    """清空并复原插件筛选规则注册表，避免测试间相互污染。"""
    original_rules = dict(plugin_filter_rule_registry._rules)
    original_groups = dict(plugin_filter_rule_registry._groups)
    plugin_filter_rule_registry.clear()
    try:
        yield
    finally:
        plugin_filter_rule_registry.clear()
        plugin_filter_rule_registry._rules.update(original_rules)
        plugin_filter_rule_registry._groups.update(original_groups)


@pytest.fixture(autouse=True)
def _isolate_torrent_analysis_port() -> Iterator[None]:
    """清空并复原分析能力端口。

    端口注入后过滤模块改走多播收集全部分析器的判定，被测的模块实例不在分发目录里，
    它自己的判定就不会参与。本文件验证的是规则集组装与筛选结果，须固定走模块内置
    分析器这一条路径，不受其它用例是否装配过分发的影响。
    """
    original_provider = torrent_analysis_port._provider
    torrent_analysis_port.reset()
    try:
        yield
    finally:
        torrent_analysis_port._provider = original_provider


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _RuleHelper:
    """过滤模块测试用的轻量规则仓库，避免依赖真实系统配置。"""

    def __init__(self, groups=None, custom_rules=None):
        """保存测试规则组与用户自定义规则。"""
        self._groups = groups or []
        self._custom_rules = custom_rules or []

    def get_custom_rules(self):
        """返回用户配置的自定义规则。"""
        return self._custom_rules

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        """按名称返回测试规则组。"""
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


def _build_filter_module(rule_string: str, custom_rules=None) -> FilterModule:
    """构造绑定轻量规则仓库并完成初始化的过滤模块。"""
    module = FilterModule()
    module.rulehelper = _RuleHelper(
        groups=[SimpleNamespace(name="test", rule_string=rule_string)],
        custom_rules=custom_rules,
    )
    module.init_module()
    return module


class _FilterRulePlugin:
    """声明筛选规则与规则组的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "筛选规则插件"

    def __init__(self, enabled=True, rules=None, groups=None, raise_error=False):
        self._enabled = enabled
        self._rules = rules
        self._groups = groups
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_filter_rules(self):
        """返回声明的筛选规则，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明筛选规则时出错")
        return self._rules

    def provides_filter_rule_groups(self):
        """返回声明的筛选规则组。"""
        return self._groups


# ---------------------------------------------------------------- 契约校验：规则ID文法


@pytest.mark.parametrize(
    "rule_id",
    ["BLU", "4K", "1080P", "H265", "60FPS", "BITRATE320", "A", "3D", "12AB34"],
)
def test_valid_rule_ids_pass_grammar_check(rule_id: str):
    """合文法的规则ID应通过校验。"""
    assert is_valid_rule_id(rule_id)


@pytest.mark.parametrize(
    "rule_id",
    [
        "MY_RULE",     # 下划线
        "MY-RULE",     # 连字符
        "MY RULE",     # 空格
        "320",         # 纯数字
        "中文规则",     # 非 ASCII
        "_LEADING",    # 下划线开头
        "RULE!",       # 操作符
        "RULE.SUB",    # 点号
        "",            # 空串
    ],
)
def test_invalid_rule_ids_fail_grammar_check(rule_id: str):
    """不合文法的规则ID应被校验拒绝。"""
    assert not is_valid_rule_id(rule_id)


@pytest.mark.parametrize(
    "rule_id",
    ["BLU", "4K", "1080P", "H265", "60FPS", "BITRATE320", "A", "3D", "12AB34"],
)
def test_valid_rule_ids_parse_as_single_atom(rule_id: str):
    """合文法的规则ID必须能被 RuleParser 当作单个原子完整解析。"""
    parsed = RuleParser().parse(rule_id).as_list()

    assert parsed == [rule_id]


@pytest.mark.parametrize("rule_id", ["MY_RULE", "MY-RULE", "RULE.SUB", "320", "中文规则"])
def test_invalid_rule_ids_are_not_parsed_as_single_atom(rule_id: str):
    """不合文法的规则ID要么解析失败，要么解析结果与原串不同——两者都会让用户困惑。"""
    try:
        parsed = RuleParser().parse(rule_id).as_list()
    except Exception:
        return
    assert parsed != [rule_id]


def test_grammar_check_agrees_with_rule_parser_on_builtin_rule_ids():
    """全部内建规则ID都应合文法，否则校验口径与既有事实矛盾。"""
    invalid = [rule_id for rule_id in BUILTIN_RULE_SET if not is_valid_rule_id(rule_id)]

    assert invalid == []


def test_plugin_declared_rule_id_is_usable_inside_a_rule_string():
    """插件规则ID写进规则组后必须能被解析出来，这正是登记时卡文法的目的。"""
    parsed = RuleParser().parse("ACMEWEB & 4K & !BLU").as_list()[0]

    assert "ACMEWEB" in str(parsed)


# ---------------------------------------------------------------- 契约校验：规则声明


def test_rule_declaration_with_illegal_id_is_rejected():
    """规则ID不合文法的声明必须在登记时被拒绝，而不是等到用户引用时才解析失败。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="MY_RULE", name="我的规则", include="Acme")
    )

    assert violation is not None
    assert "MY_RULE" in violation


def test_rule_declaration_with_numeric_only_id_is_rejected():
    """纯数字规则ID不合原子文法，必须被拒绝。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="320", name="纯数字", include="Acme")
    )

    assert violation is not None


def test_rule_declaration_without_conditions_is_rejected():
    """不带任何匹配条件的规则对每颗种子都判定通过，等同于没有这条规则。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="EMPTY", name="空规则")
    )

    assert violation is not None
    assert "匹配条件" in violation


def test_rule_declaration_without_name_is_rejected():
    """未声明展示名称的规则应被拒绝。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="NONAME", include="Acme")
    )

    assert violation is not None


def test_rule_declaration_with_uncompilable_regex_is_rejected():
    """无法编译的正则会在逐条匹配时抛异常，必须在登记时拦下。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="BADRE", name="坏正则", include="(unclosed")
    )

    assert violation is not None
    assert "无法编译" in violation


@pytest.mark.parametrize("size_range", ["abc", "1024-", ">x", "1024-2048-4096"])
def test_rule_declaration_with_malformed_size_range_is_rejected(size_range: str):
    """大小范围无法转换成数值会在匹配时抛异常，必须在登记时拦下。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="BADSIZE", name="坏大小", size_range=size_range)
    )

    assert violation is not None


def test_rule_declaration_with_non_numeric_seeders_is_rejected():
    """做种人数无法转换成整数会在匹配时抛异常，必须在登记时拦下。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="BADSEED", name="坏做种", seeders="很多")
    )

    assert violation is not None


def test_rule_declaration_with_non_string_condition_is_rejected():
    """条件字段必须是字符串，形状不对的声明应被拒绝。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(rule_id="BADTYPE", name="坏类型", include=["Acme"])
    )

    assert violation is not None


def test_valid_rule_declaration_passes():
    """条件齐备且合文法的声明应通过校验。"""
    violation = filter_rule_declaration_violation(
        FilterRuleDeclaration(
            rule_id="ACMEWEB",
            name="Acme WEB-DL",
            include=r"Acme.*WEB-?DL",
            exclude="HDTV",
            size_range="1024-8192",
            seeders="5",
            publish_time="60-1440",
        )
    )

    assert violation is None


# ---------------------------------------------------------------- 契约校验：规则组声明


@pytest.mark.parametrize(
    "rule_string",
    [
        "ACME_WEB & 4K",        # 原子不合规则ID文法
        "(ACMEWEB & 4K",        # 括号不配对
        "ACMEWEB > > 4K",       # 空的优先级层级
        "!&|",                  # 不含任何规则ID
    ],
)
def test_rule_group_declaration_with_unparseable_rule_string_is_rejected(rule_string: str):
    """规则串无法解析的规则组必须在登记时被拒绝。"""
    violation = filter_rule_group_declaration_violation(
        FilterRuleGroupDeclaration(name="坏规则组", rule_string=rule_string)
    )

    assert violation is not None


def test_rule_group_declaration_without_name_is_rejected():
    """未声明组名的规则组应被拒绝——四个使用场景保存的就是组名。"""
    violation = filter_rule_group_declaration_violation(
        FilterRuleGroupDeclaration(rule_string="ACMEWEB")
    )

    assert violation is not None


def test_valid_rule_group_declaration_passes():
    """规则串能解析的规则组应通过校验。"""
    violation = filter_rule_group_declaration_violation(
        FilterRuleGroupDeclaration(
            name="Acme 高码率优先",
            rule_string="ACMEWEB & 4K > ACMEWEB & 1080P",
            media_type="电影",
        )
    )

    assert violation is None


def test_rule_group_declaration_may_reference_not_yet_registered_rules():
    """规则组可以引用内建、用户或另一插件的规则，登记时不校验标识是否已存在。"""
    violation = filter_rule_group_declaration_violation(
        FilterRuleGroupDeclaration(name="引用未知规则", rule_string="NOTYETHERE")
    )

    assert violation is None


@pytest.mark.parametrize("rule_string", ["ACMEWEB & 4K > ACMEWEB & 1080P", "!(BLU | REMUX)"])
def test_accepted_rule_strings_are_parseable_by_rule_parser(rule_string: str):
    """通过契约校验的规则串必须真的能被 RuleParser 逐层解析。"""
    assert rule_string_violation(rule_string) is None
    for level in rule_string.split(">"):
        RuleParser().parse(level.strip())


# ---------------------------------------------------------------- 投影：扩展级裁决


def test_projection_accepts_valid_declarations():
    """契约合规的规则与规则组声明应被接受。"""
    plugin = _FilterRulePlugin(
        rules=[FilterRuleDeclaration(rule_id="ACMEWEB", name="Acme", include="Acme")],
        groups=[FilterRuleGroupDeclaration(name="Acme 优先", rule_string="ACMEWEB")],
    )
    projection = PluginProjection({"AcmePlugin": plugin})

    rules = projection.provided_filter_rules()
    groups = projection.provided_filter_rule_groups()

    assert rules["AcmePlugin"][0].rule_id == "ACMEWEB"
    assert groups["AcmePlugin"][0].name == "Acme 优先"


def test_projection_skips_only_the_offending_declaration():
    """单条声明不合契约只跳过该条，不影响同一实例的其余声明。"""
    plugin = _FilterRulePlugin(
        rules=[
            FilterRuleDeclaration(rule_id="BAD_ID", name="坏的", include="x"),
            FilterRuleDeclaration(rule_id="GOODID", name="好的", include="y"),
        ]
    )
    projection = PluginProjection({"AcmePlugin": plugin})

    accepted = projection.provided_filter_rules()["AcmePlugin"]

    assert [item.rule_id for item in accepted] == ["GOODID"]


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明规则抛异常时不应影响其它插件的投影结果。"""
    broken = _FilterRulePlugin(raise_error=True)
    healthy = _FilterRulePlugin(
        rules=[FilterRuleDeclaration(rule_id="OKRULE", name="好的", include="ok")]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_filter_rules()

    assert "Broken" not in declared
    assert declared["Ok"][0].rule_id == "OKRULE"


def test_plugin_base_subclass_without_overrides_declares_nothing():
    """什么都不声明的插件行为与既有完全一致：两个新钩子都不算已实现。"""
    from app.sdk.extension import _PluginBase
    from app.runtime.extensions.contract.extension import supports_extension_hook

    class _SilentPlugin(_PluginBase):
        """未覆写任何筛选规则钩子的插件。"""

        def init_plugin(self, config: dict = None) -> None:
            """生效配置信息。"""

        def get_state(self) -> bool:
            """返回插件启用状态。"""
            return True

        @staticmethod
        def get_command() -> List[dict]:
            """不提供命令。"""
            return []

        def get_api(self) -> List[dict]:
            """不提供 API。"""
            return []

        def get_form(self):
            """不提供配置界面。"""
            return [], {}

        def get_page(self) -> List[dict]:
            """不提供页面。"""
            return []

        def stop_service(self) -> None:
            """无后台服务需要停止。"""

    assert not supports_extension_hook(_SilentPlugin, "provides_filter_rules")
    assert not supports_extension_hook(_SilentPlugin, "provides_filter_rule_groups")


def test_projection_skips_disabled_plugin():
    """未启用的插件不应交出任何规则声明。"""
    plugin = _FilterRulePlugin(
        enabled=False,
        rules=[FilterRuleDeclaration(rule_id="ACMEWEB", name="Acme", include="Acme")],
    )
    projection = PluginProjection({"AcmePlugin": plugin})

    assert projection.provided_filter_rules() == {}


def test_same_plugin_multiple_instances_register_rule_id_once():
    """规则标识是扩展级事实，同插件多实例声明同一标识只登记一次。"""
    declaration = FilterRuleDeclaration(rule_id="ACMEWEB", name="Acme", include="Acme")
    projection = PluginProjection({
        "AcmePlugin": _FilterRulePlugin(rules=[declaration]),
        "AcmePlugin@second": _FilterRulePlugin(rules=[declaration]),
    })

    declared = projection.provided_filter_rules()

    claimants = [key for key, items in declared.items() if items]
    assert claimants == ["AcmePlugin"]


def test_same_plugin_multiple_instances_register_rule_group_once():
    """规则组名同理是扩展级事实，同插件多实例声明同一组名只登记一次。"""
    declaration = FilterRuleGroupDeclaration(name="Acme 优先", rule_string="ACMEWEB")
    projection = PluginProjection({
        "AcmePlugin": _FilterRulePlugin(groups=[declaration]),
        "AcmePlugin@second": _FilterRulePlugin(groups=[declaration]),
    })

    declared = projection.provided_filter_rule_groups()

    claimants = [key for key, items in declared.items() if items]
    assert claimants == ["AcmePlugin"]


def test_projection_normalises_declaration_into_custom_rule_shape():
    """投影结果的形状必须与用户自定义规则完全一致，规则引擎才分辨不出来源。"""
    rule_id, definition = PluginProjection.declared_filter_rule(
        FilterRuleDeclaration(rule_id="ACMEWEB", name="Acme", include="Acme")
    )

    assert rule_id == "ACMEWEB"
    assert set(definition) == {
        "id", "name", "include", "exclude", "size_range", "seeders", "publish_time",
    }
    assert definition["include"] == "Acme"


# ---------------------------------------------------------------- 注册表：跨插件冲突


def test_registry_exposes_rules_from_a_single_plugin():
    """单个插件登记的规则应原样交出。"""
    registry = PluginFilterRuleRegistry()
    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])

    assert registry.rule_definitions() == {"ACMEWEB": {"include": "Acme"}}


def test_registry_drops_rule_id_claimed_by_two_plugins():
    """跨插件同规则标识时两边都不交出，不按登记顺序静默取其一。"""
    registry = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])

    assert "SHARED" not in registry.rule_definitions()


def test_registry_conflict_verdict_is_independent_of_registration_order():
    """冲突处置只取决于「有几个插件声明了它」，与登记顺序无关。"""
    forward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    forward.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    forward.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])

    backward = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    backward.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])
    backward.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])

    assert forward.rule_definitions() == backward.rule_definitions() == {}


def test_registry_conflict_does_not_affect_other_rules():
    """冲突只作废争用的那一个标识，双方其余规则照常生效。"""
    registry = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    registry.register(
        "AcmePlugin",
        rules=[("SHARED", {"include": "acme"}), ("ACMEONLY", {"include": "a"})],
    )
    registry.register(
        "OtherPlugin",
        rules=[("SHARED", {"include": "other"}), ("OTHERONLY", {"include": "o"})],
    )

    definitions = registry.rule_definitions()

    assert set(definitions) == {"ACMEONLY", "OTHERONLY"}


def test_registry_conflict_warns_once():
    """同一冲突只告警一次，避免高频取用时刷屏。"""
    warnings: List[str] = []
    registry = PluginFilterRuleRegistry(log=SimpleNamespace(warning=warnings.append))
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])

    registry.rule_definitions()
    registry.rule_definitions()

    assert len(warnings) == 1
    assert "SHARED" in warnings[0]


def test_registry_conflict_resolves_once_one_side_goes_away():
    """冲突方停用后剩下的一方应重新生效。"""
    registry = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    registry.register("AcmePlugin", rules=[("SHARED", {"include": "acme"})])
    registry.register("OtherPlugin", rules=[("SHARED", {"include": "other"})])
    assert registry.rule_definitions() == {}

    registry.unregister_owner("OtherPlugin")

    assert registry.rule_definitions() == {"SHARED": {"include": "acme"}}


def test_registry_drops_rule_group_claimed_by_two_plugins():
    """规则组名的冲突处置与规则标识相同。"""
    registry = PluginFilterRuleRegistry(log=SimpleNamespace(warning=lambda *_: None))
    registry.register("AcmePlugin", groups=[("共享组", {"rule_string": "A"})])
    registry.register("OtherPlugin", groups=[("共享组", {"rule_string": "B"})])

    assert registry.rule_group_definitions() == {}


def test_registry_revision_tracks_content_changes():
    """登记内容变化时版本号递增，内容不变时不递增。"""
    registry = PluginFilterRuleRegistry()
    start = registry.revision

    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])
    after_register = registry.revision
    registry.register("AcmePlugin", rules=[("ACMEWEB", {"include": "Acme"})])
    after_noop = registry.revision
    registry.unregister_owner("AcmePlugin")
    after_revoke = registry.revision

    assert after_register > start
    assert after_noop == after_register
    assert after_revoke > after_noop


def test_registry_register_replaces_previous_declarations_of_the_same_owner():
    """同一实例重新登记应整体替换，声明缩减后旧登记不残留。"""
    registry = PluginFilterRuleRegistry()
    registry.register("AcmePlugin", rules=[("OLDRULE", {"include": "old"})])

    registry.register("AcmePlugin", rules=[("NEWRULE", {"include": "new"})])

    assert set(registry.rule_definitions()) == {"NEWRULE"}


# ---------------------------------------------------------------- 规则集组装与三级优先级


def test_plugin_rule_enters_rule_set_and_takes_effect():
    """插件登记的规则应进入运行期规则集，并真的筛掉不匹配的种子。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[("ACMEWEB", {"id": "ACMEWEB", "name": "Acme", "include": "Acme"})],
    )
    module = _build_filter_module(rule_string="ACMEWEB")
    matched = TorrentInfo(title="Acme Show 1080p", description="")
    unmatched = TorrentInfo(title="Other Show 1080p", description="")

    assert "ACMEWEB" in module.rule_set
    filtered = module.filter_torrents(
        rule_groups=["test"], torrent_list=[matched, unmatched]
    )

    assert filtered == [matched]


def test_plugin_rule_overrides_builtin_definition():
    """插件规则覆盖内建同名规则：内建 < 插件。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[("BLU", {"id": "BLU", "name": "插件蓝光", "include": "PluginBluRay"})],
    )
    module = _build_filter_module(rule_string="BLU")

    assert module.rule_set["BLU"]["include"] == "PluginBluRay"
    assert module.builtin_rule_set["BLU"] != module.rule_set["BLU"]


def test_user_rule_overrides_plugin_definition():
    """用户自定义规则覆盖插件同名规则：插件 < 用户。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[("SHARED", {"id": "SHARED", "name": "插件版", "include": "FromPlugin"})],
    )
    module = _build_filter_module(
        rule_string="SHARED",
        custom_rules=[
            SimpleNamespace(
                id="SHARED",
                name="用户版",
                model_dump=lambda: {
                    "id": "SHARED", "name": "用户版", "include": "FromUser",
                },
            )
        ],
    )

    assert module.rule_set["SHARED"]["include"] == "FromUser"


def test_three_tier_priority_is_builtin_then_plugin_then_user():
    """三层规则各自生效且次序为内建 < 插件 < 用户。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[
            ("BLU", {"id": "BLU", "include": "PluginBluRay"}),
            ("ACMEWEB", {"id": "ACMEWEB", "include": "Acme"}),
        ],
    )
    module = _build_filter_module(
        rule_string="ACMEWEB",
        custom_rules=[
            SimpleNamespace(
                id="BLU",
                name="用户蓝光",
                model_dump=lambda: {"id": "BLU", "include": "UserBluRay"},
            )
        ],
    )

    # 内建独有的规则仍在
    assert module.rule_set["4K"] == BUILTIN_RULE_SET["4K"]
    # 插件独有的规则已并入
    assert module.rule_set["ACMEWEB"]["include"] == "Acme"
    # 内建与插件与用户三方争同一标识时用户赢
    assert module.rule_set["BLU"]["include"] == "UserBluRay"


def test_composing_rule_set_does_not_mutate_builtin_definitions():
    """组装规则集不得改动内建规则常量本身。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("BLU", {"id": "BLU", "include": "PluginBluRay"})]
    )
    original = dict(BUILTIN_RULE_SET["BLU"])

    _build_filter_module(rule_string="BLU")

    assert BUILTIN_RULE_SET["BLU"] == original


# ---------------------------------------------------------------- 热重载与停用回收


def test_plugin_rule_disappears_after_owner_is_revoked():
    """插件停用后其规则必须从运行期规则集消失，不能残留在内存里。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("ACMEWEB", {"id": "ACMEWEB", "include": "Acme"})]
    )
    module = _build_filter_module(rule_string="ACMEWEB")
    assert "ACMEWEB" in module.rule_set

    plugin_filter_rule_registry.unregister_owner("AcmePlugin")
    module.filter_torrents(
        rule_groups=["test"], torrent_list=[TorrentInfo(title="Acme", description="")]
    )

    assert "ACMEWEB" not in module.rule_set


def test_reload_after_plugin_revoked_leaves_no_residue():
    """重载走的是同一条组装路径，插件规则撤销后重载不得残留。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("ACMEWEB", {"id": "ACMEWEB", "include": "Acme"})]
    )
    module = _build_filter_module(rule_string="ACMEWEB")
    assert "ACMEWEB" in module.rule_set

    plugin_filter_rule_registry.unregister_owner("AcmePlugin")
    module.init_module()

    assert "ACMEWEB" not in module.rule_set
    assert module.rule_set == module.builtin_rule_set


def test_builtin_definition_returns_after_plugin_override_is_revoked():
    """插件覆盖内建规则后停用，内建定义必须回来。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("BLU", {"id": "BLU", "include": "PluginBluRay"})]
    )
    module = _build_filter_module(rule_string="BLU")
    assert module.rule_set["BLU"]["include"] == "PluginBluRay"

    plugin_filter_rule_registry.unregister_owner("AcmePlugin")
    module.init_module()

    assert module.rule_set["BLU"] == BUILTIN_RULE_SET["BLU"]


def test_newly_registered_plugin_rule_is_picked_up_without_module_reload():
    """插件启停不经过模块重载路径，规则集仍须在下一次判定前刷新。"""
    module = _build_filter_module(rule_string="ACMEWEB")
    assert "ACMEWEB" not in module.rule_set

    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("ACMEWEB", {"id": "ACMEWEB", "include": "Acme"})]
    )
    matched = TorrentInfo(title="Acme Show", description="")
    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[matched])

    assert "ACMEWEB" in module.rule_set
    assert filtered == [matched]


def test_user_rules_survive_plugin_registry_refresh():
    """插件登记变化触发的重组装不得丢掉用户自定义规则。"""
    module = _build_filter_module(
        rule_string="MYRULE",
        custom_rules=[
            SimpleNamespace(
                id="MYRULE",
                name="用户规则",
                model_dump=lambda: {"id": "MYRULE", "include": "Mine"},
            )
        ],
    )

    plugin_filter_rule_registry.register(
        "AcmePlugin", rules=[("ACMEWEB", {"id": "ACMEWEB", "include": "Acme"})]
    )
    module.filter_torrents(
        rule_groups=["test"], torrent_list=[TorrentInfo(title="Mine", description="")]
    )

    assert module.rule_set["MYRULE"]["include"] == "Mine"
    assert "ACMEWEB" in module.rule_set


# ---------------------------------------------------------------- 插件管理器生命周期


class _FakeFilterRulePlugin:
    """声明筛选规则与规则组的插件桩，用于驱动插件管理器完整生命周期。"""

    plugin_name = "假想筛选规则插件"
    plugin_version = "1.0.0"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_filter_rules(self) -> Optional[List[FilterRuleDeclaration]]:
        """返回声明的固定筛选规则。"""
        return [
            FilterRuleDeclaration(rule_id="ACMEWEB", name="Acme", include=r"Acme.*WEB")
        ]

    def provides_filter_rule_groups(self) -> Optional[List[FilterRuleGroupDeclaration]]:
        """返回声明的固定筛选规则组。"""
        return [
            FilterRuleGroupDeclaration(name="Acme 优先", rule_string="ACMEWEB & 4K > ACMEWEB")
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_plugin_manager_lifecycle_registers_and_revokes_filter_rules(
    monkeypatch, plugin_manager: PluginManager
):
    """插件启动后应登记声明的规则与规则组；停止后必须撤销，不留残留。"""
    plugin_id = _FakeFilterRulePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeFilterRulePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    assert "ACMEWEB" in plugin_filter_rule_registry.rule_definitions()
    assert "Acme 优先" in plugin_filter_rule_registry.rule_group_definitions()

    plugin_manager.stop(plugin_id)

    assert plugin_filter_rule_registry.rule_definitions() == {}
    assert plugin_filter_rule_registry.rule_group_definitions() == {}


def test_plugin_manager_config_update_resyncs_filter_rule_registration(
    monkeypatch, plugin_manager: PluginManager
):
    """配置生效后停用实例应撤销登记，重新启用后登记应恢复。"""
    plugin_id = _FakeFilterRulePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeFilterRulePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)
    assert "ACMEWEB" in plugin_filter_rule_registry.rule_definitions()

    plugin_obj = plugin_manager._running_plugins[plugin_id]
    plugin_obj.enabled = False
    plugin_manager.init_plugin(plugin_id, {})
    assert plugin_filter_rule_registry.rule_definitions() == {}

    plugin_obj.enabled = True
    plugin_manager.init_plugin(plugin_id, {})
    assert "ACMEWEB" in plugin_filter_rule_registry.rule_definitions()


def test_plugin_manager_start_survives_broken_filter_rule_declaration(
    monkeypatch, plugin_manager: PluginManager
):
    """插件的 provides_filter_rules 抛异常时不应阻断插件加载。"""

    class _BrokenFilterRulePlugin(_FakeFilterRulePlugin):
        """声明筛选规则时抛异常的插件桩。"""

        def provides_filter_rules(self):
            """模拟插件实现出错。"""
            raise RuntimeError("声明筛选规则时出错")

    plugin_id = _BrokenFilterRulePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_BrokenFilterRulePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    assert plugin_id in plugin_manager._running_plugins
    assert plugin_filter_rule_registry.rule_definitions() == {}


# ---------------------------------------------------------------- 规则组的四个使用场景


def _rule_helper_with_user_groups(monkeypatch, user_groups):
    """构造读取指定用户规则组配置的 RuleHelper。"""
    from app.application import rules as rules_module

    monkeypatch.setattr(
        rules_module,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: user_groups),  # noqa: ARG005
    )
    return rules_module.RuleHelper()


def test_plugin_rule_group_is_listed_alongside_user_groups(monkeypatch):
    """插件规则组应与用户规则组一同出现在可用规则组列表里。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        groups=[("Acme 优先", {"name": "Acme 优先", "rule_string": "ACMEWEB"})],
    )
    helper = _rule_helper_with_user_groups(
        monkeypatch, [{"name": "我的组", "rule_string": "4K"}]
    )

    names = [group.name for group in helper.get_rule_groups()]

    assert "Acme 优先" in names
    assert "我的组" in names


def test_user_rule_group_overrides_plugin_group_of_the_same_name(monkeypatch):
    """同名规则组以用户配置为准：插件 < 用户。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        groups=[("共享组", {"name": "共享组", "rule_string": "ACMEWEB"})],
    )
    helper = _rule_helper_with_user_groups(
        monkeypatch, [{"name": "共享组", "rule_string": "1080P"}]
    )

    group = helper.get_rule_group("共享组")

    assert group.rule_string == "1080P"


@pytest.mark.parametrize(
    "scenario",
    [
        "UserFilterRuleGroups",
        "SearchFilterRuleGroups",
        "SubscribeFilterRuleGroups",
        "BestVersionFilterRuleGroups",
    ],
)
def test_plugin_rule_group_is_resolvable_in_every_scenario(monkeypatch, scenario: str):
    """四个使用场景保存的都是组名，插件规则组必须都能按名称解析出来。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        groups=[(
            "Acme 优先",
            {"name": "Acme 优先", "rule_string": "ACMEWEB", "media_type": None,
             "category": None},
        )],
    )
    helper = _rule_helper_with_user_groups(monkeypatch, [])
    # 每个场景保存的组名列表都会原样进入 get_rule_group_by_media 的 group_names
    configured_group_names = ["Acme 优先"]

    resolved = helper.get_rule_group_by_media(
        media=MediaInfo(), group_names=configured_group_names
    )

    assert [group.name for group in resolved] == ["Acme 优先"], scenario


def test_plugin_rule_group_reaches_filter_module_through_the_helper(monkeypatch):
    """插件规则组经规则仓库进入过滤模块后应真的按其规则串筛选。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[("ACMEWEB", {"id": "ACMEWEB", "include": r"Acme.*WEB"})],
        groups=[(
            "Acme 优先",
            {"name": "Acme 优先", "rule_string": "ACMEWEB", "media_type": None,
             "category": None},
        )],
    )
    helper = _rule_helper_with_user_groups(monkeypatch, [])
    module = FilterModule()
    module.rulehelper = helper
    module.init_module()

    matched = TorrentInfo(title="Acme Show WEB-DL", description="")
    unmatched = TorrentInfo(title="Other Show HDTV", description="")
    filtered = module.filter_torrents(
        rule_groups=["Acme 优先"], torrent_list=[matched, unmatched]
    )

    assert filtered == [matched]


# ---------------------------------------------------------------- Rust 快路


def test_plugin_rules_run_through_the_rust_fast_path():
    """插件规则是数据，必须照常走 Rust 快路，不得掉进 Python 兜底。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[(
            "ACMEWEB",
            {"id": "ACMEWEB", "name": "Acme", "include": r"Acme.*WEB", "exclude": "HDTV"},
        )],
    )
    module = _build_filter_module(rule_string="ACMEWEB")

    def fail_python_fallback(*_args, **_kwargs):
        """入口若仍调用 Python 私有匹配逻辑，测试应立即失败。"""
        raise AssertionError("插件规则不应掉进 Python 兜底")

    module._FilterModule__match_orders_with_python = fail_python_fallback

    matched = TorrentInfo(title="Acme Show WEB-DL 1080p", description="")
    unmatched = TorrentInfo(title="Acme Show HDTV", description="")
    filtered = module.filter_torrents(
        rule_groups=["test"], torrent_list=[matched, unmatched]
    )

    assert filtered == [matched]
    assert matched.pri_order == 100


def test_plugin_rule_size_range_reaches_the_rust_entry():
    """带 size_range 的插件规则应触发 MetaInfo 选项传参并由 Rust 判定。"""
    plugin_filter_rule_registry.register(
        "AcmePlugin",
        rules=[("BIGONE", {"id": "BIGONE", "name": "大文件", "size_range": "100-400"})],
    )
    module = _build_filter_module(rule_string="BIGONE")

    def fail_python_fallback(*_args, **_kwargs):
        """入口若仍调用 Python 私有匹配逻辑，测试应立即失败。"""
        raise AssertionError("插件规则不应掉进 Python 兜底")

    module._FilterModule__match_orders_with_python = fail_python_fallback

    in_range = TorrentInfo(title="Show S01E01", description="", size=200 * 1024 * 1024)
    out_of_range = TorrentInfo(title="Show S01E02", description="", size=900 * 1024 * 1024)
    filtered = module.filter_torrents(
        rule_groups=["test"], torrent_list=[in_range, out_of_range]
    )

    assert filtered == [in_range]


def test_plugin_and_user_rules_are_indistinguishable_to_the_rule_engine():
    """插件规则与用户规则形状一致，同一条数据放在哪一层筛选结果都相同。"""
    definition = {"id": "SAME", "name": "同一条", "include": r"Acme.*WEB"}
    torrents = [
        TorrentInfo(title="Acme Show WEB-DL", description=""),
        TorrentInfo(title="Other Show", description=""),
    ]

    plugin_filter_rule_registry.register("AcmePlugin", rules=[("SAME", definition)])
    from_plugin = _build_filter_module(rule_string="SAME").filter_torrents(
        rule_groups=["test"], torrent_list=list(torrents)
    )

    plugin_filter_rule_registry.unregister_owner("AcmePlugin")
    from_user = _build_filter_module(
        rule_string="SAME",
        custom_rules=[
            SimpleNamespace(id="SAME", name="同一条", model_dump=lambda: definition)
        ],
    ).filter_torrents(rule_groups=["test"], torrent_list=list(torrents))

    assert [item.title for item in from_plugin] == [item.title for item in from_user]
    assert [item.title for item in from_plugin] == ["Acme Show WEB-DL"]
