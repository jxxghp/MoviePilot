"""插件声明远程命令链路测试：命令词文法对拍、契约校验、两条来源优先级、冲突处置与停用回收。"""

import json
import re
import threading
from dataclasses import fields
from types import SimpleNamespace
from typing import Iterator, List, Optional

import pytest

from app.runtime.command import Command
from app.modules.discord.discord import Discord
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.admission.command_arbitration import BuiltinCommandArbiter
from app.runtime.extensions.registry.command import (
    PluginCommandRegistry,
    plugin_command_registry,
)
from app.runtime.extensions.contract.declaration import CommandDeclaration
from app.runtime.extensions.projection.command import PluginCommandTable
from app.runtime.extensions.admission.command import (
    command_declaration_violation,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.schemas.command import (
    AI_COMMAND_PREFIX,
    COMMAND_WORD_GRAMMAR_HINT,
    command_word_violation,
    is_valid_command_word,
)


@pytest.fixture(autouse=True)
def _isolate_command_registry() -> Iterator[None]:
    """清空并复原插件命令注册表，避免测试间相互污染。"""
    original = dict(plugin_command_registry._commands)
    plugin_command_registry.clear()
    try:
        yield
    finally:
        plugin_command_registry.clear()
        plugin_command_registry._commands.update(original)


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """清空废弃告警去重记录，保证每个用例都能观察到首次告警。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


class _CommandPlugin:
    """声明远程命令的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "命令插件"

    def __init__(self, enabled=True, commands=None, legacy=None, raise_error=False):
        self._enabled = enabled
        self._commands = commands
        self._legacy = legacy
        self._raise_error = raise_error
        self.calls: List[dict] = []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_commands(self):
        """返回声明的命令，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明命令时出错")
        return self._commands

    def get_command(self):
        """返回旧钩子声明的命令描述字典列表。"""
        return self._legacy

    def handle(self, data=None):
        """记录一次命令调用。"""
        self.calls.append(data or {})


def _declaration(cmd: str, plugin: Optional[_CommandPlugin] = None, **kwargs):
    """构造一条最小可用的命令声明。"""
    impl = plugin.handle if plugin else (lambda data=None: None)
    return CommandDeclaration(cmd=cmd, name=kwargs.pop("name", "示例"), impl=impl, **kwargs)


def _build_command_chain(warnings: Optional[List[str]] = None) -> Command:
    """构造只挂内建命令与插件命令注册表的命令中枢测试对象。

    :param warnings: 收集裁决告警的列表，为空时告警丢弃
    :return: 命令中枢测试对象
    """
    chain = object.__new__(Command)
    chain._preset_commands = {
        "/version": {
            "func": lambda: None,
            "description": "当前版本",
            "category": "管理",
            "data": {},
        }
    }
    chain._plugin_table = PluginCommandTable(
        builtin_command_words=lambda: chain._preset_commands,
        event_sender=Command.send_plugin_event,
        arbiter=BuiltinCommandArbiter(
            log=SimpleNamespace(
                warning=(warnings.append if warnings is not None else (lambda _: None))
            )
        ),
    )
    chain._other_commands = {}
    chain._commands = {}
    chain._rlock = threading.RLock()
    return chain


# ---------------------------------------------------------------- 契约校验：命令词文法


@pytest.mark.parametrize(
    "cmd",
    ["/sync", "/a", "/clear_cache", "/mediaserver_sync", "/t2", "/9", "/_x", "/" + "a" * 32],
)
def test_valid_command_words_pass_grammar_check(cmd: str):
    """合文法的命令词应通过校验。"""
    assert is_valid_command_word(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "sync",              # 缺前导斜杠
        "/Sync",             # 大写字母
        "/my cmd",           # 空格
        "/my-cmd",           # 连字符
        "/中文命令",          # 非 ASCII
        "//sync",            # 重复斜杠
        "/",                 # 只有斜杠
        "/sync.sub",         # 点号
        "/" + "a" * 33,      # 超长
        "",                  # 空串
    ],
)
def test_invalid_command_words_fail_grammar_check(cmd: str):
    """不合文法的命令词应被校验拒绝。"""
    assert not is_valid_command_word(cmd)


@pytest.mark.parametrize("cmd", ["/sync", "/clear_cache", "/t2", "/" + "a" * 32])
def test_valid_command_word_survives_real_dispatch_parse(cmd: str):
    """合文法的命令词必须能被分发链路原样取出——那里按空白切分后做精确查表。"""
    event_text = f"{cmd} 一些 参数"

    assert event_text.split()[0] == cmd


@pytest.mark.parametrize("cmd", ["/my cmd", "/my\tcmd"])
def test_command_word_with_whitespace_is_truncated_by_real_dispatch_parse(cmd: str):
    """含空白的命令词被分发链路切成前半截，永远匹配不上登记的键。"""
    assert cmd.split()[0] != cmd
    assert command_word_violation(cmd)


@pytest.mark.parametrize("cmd", ["/sync", "/clear_cache", "/t2", "/_x", "/9"])
def test_valid_command_word_round_trips_through_discord_normalizer(cmd: str):
    """合文法的命令词经渠道菜单归一后必须原样还原，否则菜单回传时对不上命令表的键。"""
    normalized = Discord._normalize_slash_command_name(cmd)

    assert f"/{normalized}" == cmd


@pytest.mark.parametrize("cmd", ["/Sync", "/my cmd", "/中文命令", "/" + "a" * 33])
def test_invalid_command_word_is_mangled_or_rejected_by_discord_normalizer(cmd: str):
    """不合文法的命令词要么被渠道归一改写、要么被丢弃，两者都让用户敲不通。"""
    normalized = Discord._normalize_slash_command_name(cmd)

    assert f"/{normalized}" != cmd


@pytest.mark.parametrize("cmd", ["/sync", "/clear_cache", "/t2", "/" + "a" * 32])
def test_valid_command_word_satisfies_telegram_bot_command_rule(cmd: str):
    """Telegram 按 cmd[1:] 批量注册菜单，命令名不合其文法会让整批注册失败。"""
    assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd[1:])


@pytest.mark.parametrize("cmd", ["/sync", "/clear_cache"])
def test_valid_command_word_passes_message_gateway_gate(cmd: str):
    """消息网关只把以 / 开头且不落在智能助手前缀下的文本转成命令事件。"""
    from app.application.orchestration.message import MessageChain

    assert cmd.startswith("/")
    assert not MessageChain._has_ai_prefix(cmd)


def test_ai_prefixed_command_word_is_rejected():
    """落在智能助手前缀下的命令词永远不会被分发，须在登记时拒绝。"""
    from app.application.orchestration.message import MessageChain

    assert MessageChain._has_ai_prefix("/aisearch")
    violation = command_word_violation("/aisearch")

    assert violation and AI_COMMAND_PREFIX in violation


def test_grammar_check_agrees_with_builtin_preset_commands():
    """全部内建命令词都应合文法，否则校验口径比宿主自己的既有事实还严。"""
    preset = _preset_command_words()

    assert len(preset) >= 10
    assert [cmd for cmd in preset if not is_valid_command_word(cmd)] == []


def _preset_command_words() -> List[str]:
    """读取内建命令词，清单由组合根持有，取用它不必构造命令中枢。"""
    from app.startup.bindings.builtin_commands import builtin_commands

    return list(builtin_commands())


# ---------------------------------------------------------------- 契约校验：命令声明


def test_declaration_without_command_word_is_rejected():
    """未声明命令词的声明必须被拒绝。"""
    assert command_declaration_violation(CommandDeclaration(name="x", impl=print)) == (
        "未声明非空的命令词 cmd"
    )


def test_declaration_with_illegal_command_word_is_rejected():
    """命令词不合文法的声明必须在登记时被拒绝，而不是等到用户敲它时才失败。"""
    violation = command_declaration_violation(
        CommandDeclaration(cmd="/My-Cmd", name="x", impl=print)
    )

    assert violation and COMMAND_WORD_GRAMMAR_HINT in violation


def test_declaration_without_name_is_rejected():
    """未声明展示名称的声明必须被拒绝——渠道菜单按它渲染按钮文案。"""
    assert command_declaration_violation(CommandDeclaration(cmd="/x", impl=print)) == (
        "未声明非空的命令展示名称 name"
    )


def test_declaration_without_callable_impl_is_rejected():
    """未声明可调用实现的声明必须被拒绝。"""
    assert command_declaration_violation(CommandDeclaration(cmd="/x", name="x")) == (
        "未声明可调用的命令实现 impl"
    )
    assert command_declaration_violation(
        CommandDeclaration(cmd="/x", name="x", impl="not-callable")
    ) == "未声明可调用的命令实现 impl"


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"category": 1}, "字段 category 必须是字符串，实际是 int"),
        ({"args_description": []}, "字段 args_description 必须是字符串，实际是 list"),
        ({"show": "yes"}, "字段 show 必须是布尔值，实际是 str"),
        (
            {"overrides_builtin": "yes"},
            "字段 overrides_builtin 必须是布尔值，实际是 str",
        ),
        ({"overrides_builtin": 1}, "字段 overrides_builtin 必须是布尔值，实际是 int"),
        ({"data": [1, 2]}, "字段 data 必须是字典，实际是 list"),
        ({"data": {1: "a"}}, "字段 data 的键必须是字符串，实际含 int"),
    ],
)
def test_declaration_with_malformed_fields_is_rejected(kwargs: dict, expected: str):
    """形状不合契约的字段必须在登记时被拒绝，且原因可读。"""
    violation = command_declaration_violation(
        CommandDeclaration(cmd="/x", name="x", impl=print, **kwargs)
    )

    assert violation == expected


def test_well_formed_declaration_passes():
    """形状完整的声明应通过契约校验。"""
    declaration = CommandDeclaration(
        cmd="/acme_sync",
        name="同步",
        category="管理",
        args_description="可选目录",
        data={"scope": "all"},
        impl=print,
    )

    assert command_declaration_violation(declaration) is None


def test_declaration_data_fields_survive_json_round_trip():
    """声明的数据字段必须能 JSON 序列化往返，实现字段不参与传输。"""
    declaration = CommandDeclaration(
        cmd="/acme_sync",
        name="同步 Acme",
        category="管理",
        args_description="可选目录",
        data={"scope": "all"},
        show=False,
        overrides_builtin=True,
        impl=print,
    )

    payload = {
        field.name: getattr(declaration, field.name)
        for field in fields(declaration)
        if field.name != "impl"
    }
    payload["data"] = dict(payload["data"])
    restored = json.loads(json.dumps(payload))

    assert restored == {
        "cmd": "/acme_sync",
        "name": "同步 Acme",
        "category": "管理",
        "args_description": "可选目录",
        "data": {"scope": "all"},
        "show": False,
        "overrides_builtin": True,
    }
    rebuilt = CommandDeclaration(impl=print, **restored)
    assert rebuilt == declaration
    assert command_declaration_violation(rebuilt) is None


# ---------------------------------------------------------------- 投影：逐条隔离与实例内唯一


def test_malformed_declaration_only_skips_itself():
    """一条坏声明只跳过它自己，同一实例的其余命令照常登记。"""
    plugin = _CommandPlugin(commands=[
        _declaration("/good_one"),
        CommandDeclaration(cmd="/Bad Cmd", name="坏的", impl=print),
        _declaration("/good_two"),
    ])
    projection = PluginProjection({"AcmePlugin": plugin})

    accepted = projection.provided_commands()["AcmePlugin"]

    assert [item.cmd for item in accepted] == ["/good_one", "/good_two"]


def test_duplicate_command_word_within_one_instance_is_rejected():
    """同一实例内重复声明同一命令词时后一条被拒绝，不静默覆盖。"""
    plugin = _CommandPlugin(commands=[
        _declaration("/sync", name="第一条"),
        _declaration("/sync", name="第二条"),
        _declaration("/other"),
    ])
    projection = PluginProjection({"AcmePlugin": plugin})

    accepted = projection.provided_commands()["AcmePlugin"]

    assert [(item.cmd, item.name) for item in accepted] == [
        ("/sync", "第一条"),
        ("/other", "示例"),
    ]


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明命令时抛异常不应影响其它插件的投影结果。"""
    projection = PluginProjection({
        "Broken": _CommandPlugin(raise_error=True),
        "Ok": _CommandPlugin(commands=[_declaration("/ok")]),
    })

    declared = projection.provided_commands()

    assert "Broken" not in declared
    assert [item.cmd for item in declared["Ok"]] == ["/ok"]


def test_projection_skips_disabled_plugin():
    """未启用的插件不应交出任何命令声明。"""
    plugin = _CommandPlugin(enabled=False, commands=[_declaration("/sync")])

    assert PluginProjection({"AcmePlugin": plugin}).provided_commands() == {}


# ---------------------------------------------------------------- 同插件多分身：扩展级去重


def test_same_plugin_multiple_instances_register_command_word_once():
    """命令词是扩展级事实，同插件多分身声明同一命令词只登记一次，默认实例胜出。"""
    projection = PluginProjection({
        "AcmePlugin@second": _CommandPlugin(commands=[_declaration("/sync")]),
        "AcmePlugin": _CommandPlugin(commands=[_declaration("/sync")]),
    })

    declared = projection.provided_commands()

    assert [key for key, items in declared.items() if items] == ["AcmePlugin"]


def test_same_plugin_multiple_instances_keep_distinct_command_words():
    """同插件多分身各自声明不同命令词时互不影响，两条都登记。"""
    projection = PluginProjection({
        "AcmePlugin": _CommandPlugin(commands=[_declaration("/first")]),
        "AcmePlugin@second": _CommandPlugin(commands=[_declaration("/second")]),
    })

    declared = projection.provided_commands()

    assert [item.cmd for item in declared["AcmePlugin"]] == ["/first"]
    assert [item.cmd for item in declared["AcmePlugin@second"]] == ["/second"]


def test_sibling_instances_declaring_one_word_register_it_once_in_registry():
    """同插件多分身争同一命令词时注册表只交出一份，而不是两边都失效。"""
    registry = PluginCommandRegistry(log=SimpleNamespace(warning=lambda _: None))
    registry.register("AcmePlugin", [("/sync", {"pid": "AcmePlugin"})])
    registry.register("AcmePlugin@second", [("/sync", {"pid": "AcmePlugin@second"})])

    definitions = registry.command_definitions()

    assert definitions["/sync"]["pid"] == "AcmePlugin"


# ---------------------------------------------------------------- 跨插件冲突：双方一并失效


def test_cross_plugin_command_conflict_invalidates_both_sides():
    """不同插件声明同一命令词时双方一并失效，宿主不按登记顺序替用户挑一个。"""
    warnings: List[str] = []
    registry = PluginCommandRegistry(log=SimpleNamespace(warning=warnings.append))
    registry.register("AlphaPlugin", [
        ("/sync", {"pid": "AlphaPlugin"}),
        ("/alpha_only", {"pid": "AlphaPlugin"}),
    ])
    registry.register("BetaPlugin", [
        ("/sync", {"pid": "BetaPlugin"}),
        ("/beta_only", {"pid": "BetaPlugin"}),
    ])

    definitions = registry.command_definitions()

    assert "/sync" not in definitions
    assert set(definitions) == {"/alpha_only", "/beta_only"}
    assert len(warnings) == 1
    assert "/sync" in warnings[0]
    assert "AlphaPlugin" in warnings[0] and "BetaPlugin" in warnings[0]


def test_cross_plugin_command_conflict_warns_only_once():
    """同一冲突反复取用时只告警一次，不刷屏。"""
    warnings: List[str] = []
    registry = PluginCommandRegistry(log=SimpleNamespace(warning=warnings.append))
    registry.register("AlphaPlugin", [("/sync", {"pid": "AlphaPlugin"})])
    registry.register("BetaPlugin", [("/sync", {"pid": "BetaPlugin"})])

    registry.command_definitions()
    registry.command_definitions()

    assert len(warnings) == 1


def test_cross_plugin_conflict_resolves_when_one_side_stops():
    """冲突一方停用后另一方重新参与裁决并接手该命令词。"""
    registry = PluginCommandRegistry(log=SimpleNamespace(warning=lambda _: None))
    registry.register("AlphaPlugin", [("/sync", {"pid": "AlphaPlugin"})])
    registry.register("BetaPlugin", [("/sync", {"pid": "BetaPlugin"})])
    assert "/sync" not in registry.command_definitions()

    registry.unregister_owner("BetaPlugin")

    assert registry.command_definitions()["/sync"]["pid"] == "AlphaPlugin"


def test_conflict_diagnosis_marks_both_sides_ineffective():
    """诊断信息如实标注冲突双方都不生效，便于用户定位。"""
    registry = PluginCommandRegistry(log=SimpleNamespace(warning=lambda _: None))
    registry.register("AlphaPlugin", [("/sync", {"pid": "AlphaPlugin"})])
    registry.register("BetaPlugin", [("/sync", {"pid": "BetaPlugin"})])

    assert [entry["effective"] for entry in registry.diagnose()] == [False, False]


def test_claims_are_ordered_independently_of_registration_order():
    """声明清单按命令词排序交出，与登记先后无关，可见性入口才有确定的列举。"""
    first = PluginCommandRegistry(log=SimpleNamespace(warning=lambda _: None))
    first.register("BetaPlugin", [("/sync", {"pid": "BetaPlugin"})])
    first.register("AlphaPlugin", [("/alpha", {"pid": "AlphaPlugin"}),
                                   ("/sync", {"pid": "AlphaPlugin"})])
    second = PluginCommandRegistry(log=SimpleNamespace(warning=lambda _: None))
    second.register("AlphaPlugin", [("/sync", {"pid": "AlphaPlugin"}),
                                    ("/alpha", {"pid": "AlphaPlugin"})])
    second.register("BetaPlugin", [("/sync", {"pid": "BetaPlugin"})])

    assert first.claims() == second.claims()
    assert [claim.cmd for claim in first.claims()] == ["/alpha", "/sync"]
    assert first.claims()[1].plugins == ("AlphaPlugin", "BetaPlugin")
    assert first.claims()[1].effective is False


# ------------------------------------------------- 插件与内建同词：接管意图须显式声明


def _plugin_definition(cmd: str, pid: str = "AcmePlugin", **kwargs) -> dict:
    """构造一条已投影的插件命令定义。

    :param cmd: 命令词
    :param pid: 声明方实例键
    :param kwargs: 覆盖默认字段的取值
    :return: 命令定义字典
    """
    definition = {
        "cmd": cmd,
        "desc": "插件同步",
        "category": "插件",
        "show": True,
        "data": {},
        "impl": print,
        "overrides_builtin": False,
        "pid": pid,
    }
    definition.update(kwargs)
    return definition


def test_plugin_command_colliding_with_builtin_is_declined_without_override_intent():
    """未声明接管意图的同名插件命令作废，内建命令不受影响。"""
    arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None))

    result = arbiter.arbitrate({"/version": _plugin_definition("/version")}, ["/version"])

    assert result.effective == {}
    assert set(result.declined) == {"/version"}
    assert result.overriding == ()


def test_plugin_command_with_declared_override_intent_takes_over_the_builtin():
    """声明了接管意图的插件命令生效，用插件增强内建命令是正当诉求。"""
    arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None))
    definition = _plugin_definition("/version", overrides_builtin=True)

    result = arbiter.arbitrate({"/version": definition}, ["/version"])

    assert result.effective == {"/version": definition}
    assert result.overriding == ("/version",)
    assert result.declined == {}


def test_override_intent_on_a_word_the_host_does_not_own_changes_nothing():
    """没有同名内建命令时接管意图不产生任何差别，插件命令照常生效。"""
    arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None))
    plain = _plugin_definition("/acme_sync")
    claiming = _plugin_definition("/acme_pull", overrides_builtin=True)

    result = arbiter.arbitrate(
        {"/acme_sync": plain, "/acme_pull": claiming}, ["/version"]
    )

    assert set(result.effective) == {"/acme_sync", "/acme_pull"}
    assert result.overriding == ()
    assert result.declined == {}


def test_declined_collision_warns_once_and_names_the_remedy():
    """撞车只告警一次，且说清该声明接管意图还是改命令词。"""
    warnings: List[str] = []
    arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=warnings.append))
    table = {"/version": _plugin_definition("/version")}

    arbiter.arbitrate(table, ["/version"])
    arbiter.arbitrate(table, ["/version"])

    assert len(warnings) == 1
    assert "/version" in warnings[0]
    assert "AcmePlugin" in warnings[0]
    assert "overrides_builtin" in warnings[0]


def test_arbitration_result_does_not_depend_on_declaration_iteration_order():
    """裁决只看声明内容，同一批声明换个次序结果一致。"""
    arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None))
    claiming = _plugin_definition("/version", pid="AlphaPlugin", overrides_builtin=True)
    other = _plugin_definition("/acme_sync", pid="BetaPlugin")

    forward = arbiter.arbitrate({"/version": claiming, "/acme_sync": other}, ["/version"])
    backward = arbiter.arbitrate({"/acme_sync": other, "/version": claiming}, ["/version"])

    assert forward.effective == backward.effective
    assert forward.overriding == backward.overriding == ("/version",)


# ---------------------------------------------------------------- 两条来源：声明式优先


def test_legacy_hook_still_works_and_emits_deprecation_warning(monkeypatch):
    """get_command() 必须继续工作，并在触达时留下一次废弃告警。"""
    emitted: List[str] = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin = _CommandPlugin(legacy=[{"cmd": "/legacy", "desc": "旧命令", "data": {}}])
    projection = PluginProjection({"AcmePlugin": plugin})

    commands = projection.commands()

    assert [item["cmd"] for item in commands] == ["/legacy"]
    assert commands[0]["pid"] == "AcmePlugin"
    assert len(emitted) == 1
    assert "get_command()" in emitted[0] and "provides_commands()" in emitted[0]


def test_declaration_wins_over_legacy_hook_for_same_command_word():
    """同一实例两条来源声明同一命令词时声明式生效。"""
    plugin = _CommandPlugin(
        commands=[_declaration("/sync", name="声明式")],
        legacy=[{"cmd": "/sync", "desc": "旧写法"}, {"cmd": "/legacy_only", "desc": "旧的"}],
    )
    projection = PluginProjection({"AcmePlugin": plugin})

    commands = {item["cmd"]: item for item in projection.commands()}

    assert commands["/sync"]["desc"] == "声明式"
    assert callable(commands["/sync"]["impl"])
    assert set(commands) == {"/sync", "/legacy_only"}


def test_legacy_hook_returning_none_is_not_a_claim():
    """旧钩子返回 None 算未认领，不产生任何命令，也不触发告警。"""
    projection = PluginProjection({"AcmePlugin": _CommandPlugin()})

    assert projection.commands() == []


def test_legacy_hook_returning_empty_list_does_not_warn(monkeypatch):
    """旧钩子返回空列表表示已认领但不提供命令，不该被当成触达而留下废弃告警。"""
    emitted: List[str] = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    projection = PluginProjection({"AcmePlugin": _CommandPlugin(legacy=[])})

    assert projection.commands() == []
    assert emitted == []


# ---------------------------------------------------------------- 命令中枢：组装、调用与回收


def test_plugin_command_table_builds_commands_from_registry():
    """插件命令表按注册表组装插件命令，带实现的命令直接调用实现。"""
    plugin = _CommandPlugin()
    plugin_command_registry.register("AcmePlugin", [(
        "/sync",
        {
            "cmd": "/sync",
            "desc": "同步",
            "category": "管理",
            "show": True,
            "data": {"scope": "all"},
            "impl": plugin.handle,
            "pid": "AcmePlugin",
        },
    )])
    chain = _build_command_chain()

    chain._plugin_table.rebuild()
    built = chain._plugin_table.commands

    assert built["/sync"]["func"] == plugin.handle
    assert built["/sync"]["pid"] == "AcmePlugin"
    assert built["/sync"]["data"] == {"data": {"scope": "all"}}


def test_command_execution_passes_declared_data_and_context():
    """执行声明式命令时实现拿到声明的静态数据与本次调用的上下文。"""
    plugin = _CommandPlugin()
    plugin_command_registry.register("AcmePlugin", [(
        "/sync",
        {
            "cmd": "/sync",
            "desc": "同步",
            "show": True,
            "data": {"scope": "all"},
            "impl": plugin.handle,
            "pid": "AcmePlugin",
        },
    )])
    chain = _build_command_chain()

    chain.execute(cmd="/sync", data_str="目录", userid="u1", source="s1")

    assert plugin.calls == [{
        "scope": "all",
        "channel": None,
        "source": "s1",
        "user": "u1",
        "arg_str": "目录",
    }]


def test_stopped_plugin_command_disappears_from_lookup():
    """插件停用后其命令必须从命令表消失，用户再敲该命令得到「命令不存在」。"""
    plugin = _CommandPlugin()
    definition = {
        "cmd": "/sync",
        "desc": "同步",
        "show": True,
        "data": {},
        "impl": plugin.handle,
        "pid": "AcmePlugin",
    }
    plugin_command_registry.register("AcmePlugin", [("/sync", definition)])
    chain = _build_command_chain()
    assert chain.get("/sync")

    plugin_command_registry.unregister_owner("AcmePlugin")

    assert chain.get("/sync") == {}
    assert "/sync" not in chain.get_commands()
    assert plugin.calls == []


def test_stopped_plugin_command_does_not_execute():
    """停用后即使命令被再次触发，也不会调用到已卸载实例的实现。"""
    plugin = _CommandPlugin()
    plugin_command_registry.register("AcmePlugin", [(
        "/sync",
        {"cmd": "/sync", "desc": "同步", "show": True, "data": {},
         "impl": plugin.handle, "pid": "AcmePlugin"},
    )])
    chain = _build_command_chain()
    plugin_command_registry.unregister_owner("AcmePlugin")

    chain.execute(cmd="/sync", userid="u1")

    assert plugin.calls == []


def test_builtin_command_survives_plugin_conflict_fallback():
    """插件冲突作废的命令词若与内建命令同名，命令表回落到内建定义。"""
    plugin_command_registry.register("AlphaPlugin", [(
        "/version", {"cmd": "/version", "desc": "A", "show": True, "data": {},
                     "impl": print, "pid": "AlphaPlugin"},
    )])
    plugin_command_registry.register("BetaPlugin", [(
        "/version", {"cmd": "/version", "desc": "B", "show": True, "data": {},
                     "impl": print, "pid": "BetaPlugin"},
    )])
    chain = _build_command_chain()

    assert chain.get("/version")["description"] == "当前版本"


# ------------------------------------------------- 命令中枢：插件与内建同词的两向处置


def _register_plugin_command(cmd: str, plugin: _CommandPlugin, **kwargs) -> None:
    """把一条插件命令登记进全局注册表。

    :param cmd: 命令词
    :param plugin: 提供实现的插件桩
    :param kwargs: 覆盖默认字段的取值，含声明方实例键 pid
    :return: 无返回值
    """
    pid = kwargs.pop("pid", "AcmePlugin")
    definition = {
        "cmd": cmd,
        "desc": "插件版本",
        "category": "插件",
        "show": True,
        "data": {},
        "impl": plugin.handle,
        "overrides_builtin": False,
        "pid": pid,
    }
    definition.update(kwargs)
    plugin_command_registry.register(pid, [(cmd, definition)])


def test_plugin_command_does_not_silently_shadow_the_builtin_one():
    """未声明接管意图时敲内建命令词仍走内建，不会静默调进插件。"""
    plugin = _CommandPlugin()
    _register_plugin_command("/version", plugin)
    chain = _build_command_chain()

    chain.execute(cmd="/version", userid="u1")

    assert chain.get("/version")["description"] == "当前版本"
    assert chain.get("/version").get("pid") is None
    assert plugin.calls == []


def test_declared_override_hands_the_builtin_command_word_to_the_plugin():
    """声明了接管意图时该命令词交给插件，敲它执行插件实现。"""
    plugin = _CommandPlugin()
    _register_plugin_command("/version", plugin, overrides_builtin=True)
    chain = _build_command_chain()

    chain.execute(cmd="/version", userid="u1")

    assert chain.get("/version")["pid"] == "AcmePlugin"
    assert chain.get("/version")["description"] == "插件版本"
    assert plugin.calls == [{"channel": None, "source": None, "user": "u1"}]


def test_declined_plugin_command_is_reported_once_to_the_user_facing_log():
    """撞车只在日志里说一次，且指出该声明接管意图还是改命令词。"""
    warnings: List[str] = []
    plugin = _CommandPlugin()
    _register_plugin_command("/version", plugin)
    chain = _build_command_chain(warnings)

    chain.get("/version")
    chain.get("/version")

    assert len(warnings) == 1
    assert "/version" in warnings[0] and "overrides_builtin" in warnings[0]


def test_builtin_command_returns_when_the_overriding_plugin_stops():
    """接管方停用后内建命令立刻回来，用户敲它又得到内建行为。"""
    plugin = _CommandPlugin()
    _register_plugin_command("/version", plugin, overrides_builtin=True)
    chain = _build_command_chain()
    assert chain.get("/version")["pid"] == "AcmePlugin"

    plugin_command_registry.unregister_owner("AcmePlugin")

    assert chain.get("/version")["description"] == "当前版本"
    assert chain.get("/version").get("pid") is None


def test_two_plugins_claiming_a_builtin_word_both_lose_even_with_override_intent():
    """跨插件裁决在前：都声称要接管同一个内建命令词时双方仍一并失效。"""
    alpha, beta = _CommandPlugin(), _CommandPlugin()
    _register_plugin_command("/version", alpha, pid="AlphaPlugin", overrides_builtin=True)
    _register_plugin_command("/version", beta, pid="BetaPlugin", overrides_builtin=True)
    chain = _build_command_chain()

    assert chain.get("/version")["description"] == "当前版本"
    assert chain.get("/version").get("pid") is None


def test_legacy_hook_command_cannot_take_over_a_builtin_command():
    """废弃钩子报不出接管意图，其同名命令按撞车处置，内建命令保持生效。"""
    plugin = _CommandPlugin(legacy=[{"cmd": "/version", "desc": "旧写法", "data": {}}])
    projection = PluginProjection({"AcmePlugin": plugin})
    for command in projection.commands():
        plugin_command_registry.register("AcmePlugin", [(command["cmd"], command)])
    chain = _build_command_chain()

    assert chain.get("/version")["description"] == "当前版本"


def test_override_intent_does_not_exempt_the_command_word_from_grammar():
    """接管意图不放宽命令词文法，不合文法的声明照样在登记时被拒。"""
    declaration = CommandDeclaration(
        cmd="/Version", name="接管版本", impl=print, overrides_builtin=True
    )

    violation = command_declaration_violation(declaration)

    assert violation is not None
    assert COMMAND_WORD_GRAMMAR_HINT in violation


def test_plugin_command_table_reassembles_only_when_the_registry_revision_moves():
    """插件命令表只在登记版本变化时重组，命令中枢据此决定要不要重新合并命令表。"""
    plugin = _CommandPlugin()
    _register_plugin_command("/acme_sync", plugin)
    table = PluginCommandTable(
        builtin_command_words=lambda: (),
        event_sender=Command.send_plugin_event,
        arbiter=BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None)),
    )

    assert table.refresh() is True
    assert table.refresh() is False
    assert set(table.commands) == {"/acme_sync"}

    plugin_command_registry.unregister_owner("AcmePlugin")

    assert table.refresh() is True
    assert table.commands == {}


def test_command_table_staleness_is_decided_only_by_the_registry_revision():
    """命令表的过期判据只有登记版本号一条，没有第二条通路。"""
    plugin = _CommandPlugin()
    _register_plugin_command("/acme_sync", plugin)
    chain = _build_command_chain()
    assert chain.get("/acme_sync")

    revision = plugin_command_registry.revision
    plugin_command_registry._commands["AcmePlugin"] = {}

    assert plugin_command_registry.revision == revision
    assert chain.get("/acme_sync")

    plugin_command_registry.unregister_owner("AcmePlugin")

    assert plugin_command_registry.revision != revision
    assert chain.get("/acme_sync") == {}
