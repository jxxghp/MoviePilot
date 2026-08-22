"""插件声明智能体工具链路测试：契约校验、基类注入、聚合归属与废弃告警。"""

from typing import Iterator, List, Optional

import pytest

from app.agent.tools.base import MoviePilotTool
from app.foundation.singleton import Singleton
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.contract.declaration import AgentToolDeclaration
from app.runtime.extensions.admission import agent_tool
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager


class _ValidTool(MoviePilotTool):
    """契约合规的智能体工具桩。"""

    name: str = "valid_tool"
    description: str = "A valid demo tool."

    async def run(self, **kwargs) -> str:
        """返回固定结果。"""
        return "ok"


class _CompatTool(_ValidTool):
    """自带 name/description 的工具桩，用于验证直接交出实现类的兼容写法。"""

    name: str = "compat_tool"
    description: str = "A compat demo tool."


class _UnnamedTool(MoviePilotTool):
    """未覆盖 name/description 的工具桩，两者在实现类与声明上均取不到值。"""

    async def run(self, **kwargs) -> str:
        """返回固定结果。"""
        return "ok"


class _IncompleteTool(MoviePilotTool):
    """未实现 run 方法的工具桩，抽象方法残留。"""

    name: str = "incomplete_tool"
    description: str = "Missing run implementation."


class _SyncRunTool(MoviePilotTool):
    """run 方法为同步实现的工具桩，违反异步契约。"""

    name: str = "sync_run_tool"
    description: str = "Synchronous run implementation."

    def run(self, **kwargs) -> str:  # type: ignore[override]
        """同步返回固定结果。"""
        return "ok"


class _NotATool:
    """与工具基类无关的普通类。"""

    name = "not_a_tool"
    description = "Not a tool base subclass."

    async def run(self, **kwargs) -> str:
        """返回固定结果。"""
        return "ok"


@pytest.fixture(autouse=True)
def _isolate_agent_tool_base() -> Iterator[None]:
    """快照并复原智能体工具基类注入状态，避免测试间相互污染。"""
    original = agent_tool._agent_tool_base
    agent_tool.configure_agent_tool_base(MoviePilotTool)
    try:
        yield
    finally:
        agent_tool._agent_tool_base = original


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _CapableToolPlugin:
    """声明智能体工具的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "工具插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_agent_tools(self):
        """返回声明的智能体工具，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明智能体工具时出错")
        return self._declarations


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留，登记方为实例键。"""
    plugin = _CapableToolPlugin(
        declarations=[
            AgentToolDeclaration(
                name="valid_tool",
                description="A valid demo tool.",
                impl=_ValidTool,
            )
        ]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert len(declared["DemoTool"]) == 1
    accepted = declared["DemoTool"][0]
    assert accepted.name == "valid_tool"
    assert accepted.description == "A valid demo tool."
    assert accepted.impl is _ValidTool


def test_projection_accepts_bare_impl_class_without_wrapper():
    """插件直接交出实现类而不包 AgentToolDeclaration 的兼容写法应被接受。"""
    plugin = _CapableToolPlugin(declarations=[_CompatTool])
    projection = PluginProjection({"CompatTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["CompatTool"] == [_CompatTool]


def test_declaration_without_explicit_identity_falls_back_to_impl_defaults():
    """声明未显式携带 name/description 时回落读取实现类的 pydantic 字段默认值。"""
    plugin = _CapableToolPlugin(declarations=[AgentToolDeclaration(impl=_ValidTool)])
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == [AgentToolDeclaration(impl=_ValidTool)]
    assert agent_tool.agent_tool_declaration_violation(
        AgentToolDeclaration(impl=_ValidTool)
    ) is None


@pytest.mark.parametrize(
    "declaration",
    [
        AgentToolDeclaration(name="demo", description="demo", impl="not-a-class"),
        AgentToolDeclaration(name="demo", description="demo", impl=None),
    ],
    ids=["impl_is_string", "impl_missing"],
)
def test_projection_rejects_impl_not_a_class(declaration):
    """impl 不是类的声明必须被拒绝。"""
    plugin = _CapableToolPlugin(declarations=[declaration])
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == []


def test_projection_rejects_impl_not_tool_base_subclass():
    """impl 不是工具基类子类的声明必须被拒绝。"""
    plugin = _CapableToolPlugin(
        declarations=[
            AgentToolDeclaration(name="demo", description="demo", impl=_NotATool)
        ]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == []


def test_projection_rejects_impl_with_unimplemented_abstract_methods():
    """run 方法未落地的声明必须被拒绝。"""
    plugin = _CapableToolPlugin(
        declarations=[
            AgentToolDeclaration(name="demo", description="demo", impl=_IncompleteTool)
        ]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == []


def test_projection_rejects_declaration_without_usable_identity():
    """声明与实现均取不到非空 name/description 时必须被拒绝。"""
    plugin = _CapableToolPlugin(
        declarations=[AgentToolDeclaration(name="", description="", impl=_UnnamedTool)]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == []


def test_projection_rejects_synchronous_run():
    """run 方法为同步实现的声明必须被拒绝。"""
    plugin = _CapableToolPlugin(
        declarations=[
            AgentToolDeclaration(name="demo", description="demo", impl=_SyncRunTool)
        ]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert declared["DemoTool"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableToolPlugin(
        declarations=[
            AgentToolDeclaration(name="ok", description="ok", impl=_ValidTool),
            AgentToolDeclaration(name="", description="", impl=_UnnamedTool),
            AgentToolDeclaration(name="bad", description="bad", impl=_NotATool),
        ]
    )
    projection = PluginProjection({"DemoTool": plugin})

    declared = projection.provided_agent_tools()

    assert len(declared["DemoTool"]) == 1
    assert declared["DemoTool"][0].name == "ok"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明智能体工具抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableToolPlugin(raise_error=True)
    healthy = _CapableToolPlugin(
        declarations=[AgentToolDeclaration(name="ok", description="ok", impl=_ValidTool)]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_agent_tools()

    assert "Broken" not in declared
    assert declared["Ok"][0].name == "ok"


def test_contract_skips_inheritance_check_when_base_not_injected():
    """基类未注入时跳过继承项校验，其余各项照常生效。"""
    agent_tool._agent_tool_base = None

    violation = agent_tool.agent_tool_declaration_violation(
        AgentToolDeclaration(name="demo", description="demo", impl=_NotATool)
    )

    assert violation is None


def test_contract_rejects_non_subclass_when_base_injected():
    """基类已注入时未继承工具基类的实现必须被拒绝。"""
    violation = agent_tool.agent_tool_declaration_violation(
        AgentToolDeclaration(name="demo", description="demo", impl=_NotATool)
    )

    assert violation is not None
    assert "工具基类" in violation


def test_agent_tool_base_configuration_resolves_to_the_real_class() -> None:
    """组合根注入的工具基类必须确实是 app.agent.tools.base.MoviePilotTool。

    装配点内部使用惰性 import 取基类；若模块路径改名或搬家，import 只会在
    initialize() 的兜底 except 中留下一条与本次装配无直接关联的初始化失败日志，
    注入状态仍停留在未配置，契约校验从此对继承关系静默失去判别力。这里直接
    调用装配函数并核对注入对象的身份，把潜在的静默退化变成一次响亮的失败。
    """
    from app.startup.agent_initializer import _configure_agent_tool_contract_base

    _configure_agent_tool_contract_base()

    assert agent_tool._agent_tool_base is MoviePilotTool


class _FakeAgentToolPlugin:
    """既声明新式工具又实现旧式钩子的插件桩，用于驱动聚合器完整链路。"""

    plugin_name = "假想工具插件"
    plugin_version = "1.0.0"

    def __init__(
        self,
        declared: Optional[List[AgentToolDeclaration]] = None,
        legacy: Optional[list] = None,
        state: bool = True,
    ):
        self._declared = declared or []
        self._legacy = legacy
        self._state = state
        self.declared_calls: List[int] = []
        self.legacy_calls: List[int] = []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._state

    def provides_agent_tools(self):
        """返回声明的智能体工具并记录调用次数。"""
        self.declared_calls.append(1)
        return self._declared

    def get_agent_tools(self):
        """返回旧式裸工具类列表并记录调用次数；未配置时返回 None 表示未提供。"""
        if self._legacy is None:
            return None
        self.legacy_calls.append(1)
        return self._legacy


def test_get_plugin_agent_tools_merges_declared_and_legacy_sources(
    plugin_manager: PluginManager,
) -> None:
    """同一实例的声明式工具与旧式裸类工具应合并到同一条聚合结果中。"""
    plugin = _FakeAgentToolPlugin(
        declared=[
            AgentToolDeclaration(name="valid_tool", description="d", impl=_ValidTool)
        ],
        legacy=[_CompatTool],
    )
    plugin_manager.running_plugins["DemoPlugin"] = plugin

    result = plugin_manager.get_plugin_agent_tools()

    assert len(result) == 1
    assert result[0]["plugin_id"] == "DemoPlugin"
    assert result[0]["tools"] == [_ValidTool, _CompatTool]


def test_get_plugin_agent_tools_emits_deprecation_warning_for_legacy_hook(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """触达旧式 get_agent_tools() 时必须触发一次废弃告警，重复触达不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin_manager.running_plugins["DemoPlugin"] = _FakeAgentToolPlugin(
        legacy=[_CompatTool]
    )

    plugin_manager.get_plugin_agent_tools()
    plugin_manager.clear_plugin_agent_tools_cache()
    plugin_manager.get_plugin_agent_tools()

    assert len(emitted) == 1
    assert "get_agent_tools" in emitted[0]


def test_declared_agent_tools_are_cached(plugin_manager: PluginManager) -> None:
    """声明式工具同样经过缓存，不会在同一轮聚合内被反复询问。"""
    plugin = _FakeAgentToolPlugin(
        declared=[
            AgentToolDeclaration(name="valid_tool", description="d", impl=_ValidTool)
        ]
    )
    plugin_manager.running_plugins["DemoPlugin"] = plugin

    first = plugin_manager.get_plugin_agent_tools()
    second = plugin_manager.get_plugin_agent_tools()

    assert len(plugin.declared_calls) == 1
    assert first == second
    assert first[0]["tools"] == [_ValidTool]


def test_declared_agent_tools_cache_can_be_cleared(plugin_manager: PluginManager) -> None:
    """清理缓存后应重新读取插件当前声明的智能体工具，版本号随之推进。"""
    plugin = _FakeAgentToolPlugin(
        declared=[
            AgentToolDeclaration(name="valid_tool", description="d", impl=_ValidTool)
        ]
    )
    plugin_manager.running_plugins["DemoPlugin"] = plugin

    before_revision = plugin_manager.get_plugin_agent_tools_revision()
    assert plugin_manager.get_plugin_agent_tools()[0]["tools"] == [_ValidTool]

    plugin_manager.clear_plugin_agent_tools_cache()
    after_revision = plugin_manager.get_plugin_agent_tools_revision()
    assert plugin_manager.get_plugin_agent_tools()[0]["tools"] == [_ValidTool]

    assert after_revision != before_revision
    assert len(plugin.declared_calls) == 2
