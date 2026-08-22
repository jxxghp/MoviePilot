"""插件声明模块方法表链路测试：契约校验、与 get_module() 并存、分发接入。"""

from types import SimpleNamespace
from typing import Iterator

import pytest

from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.contract.declaration import ModuleDeclaration
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.admission import module
from app.runtime.extensions.projection import plugin as projection_module
from app.runtime.extensions.projection.plugin import PluginProjection, PluginProviderSource


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


@pytest.fixture(autouse=True)
def _clean_overlap_warnings() -> Iterator[None]:
    """每个用例前后都清空模块来源重叠告警去重记录，避免用例间互相掩盖。"""
    projection_module._module_source_overlap_warnings_seen.clear()
    yield
    projection_module._module_source_overlap_warnings_seen.clear()


class _Plugin(SimpleNamespace):
    """提供可配置插件 hook 的最小运行态插件替身。"""

    def __init__(self, enabled=True, **hooks):
        """保存启用状态、插件名称和 hook 实现。"""
        super().__init__(plugin_name=hooks.pop("plugin_name", "测试插件"), **hooks)
        self._enabled = enabled

    def get_state(self):
        """返回预设启用状态。"""
        return self._enabled

    def get_name(self):
        """返回插件展示名称。"""
        return self.plugin_name


class _CapableModulePlugin:
    """声明模块方法表的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "模块插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def provides_modules(self):
        """返回声明的模块方法表，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明模块方法表时出错")
        return self._declarations


def _handler() -> str:
    """契约校验用的最小可调用桩。"""
    return "ok"


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def test_contract_accepts_valid_declaration() -> None:
    """方法表非空、键值均合法时声明合规。"""
    declaration = ModuleDeclaration(methods={"recognize": _handler})

    assert module.module_declaration_violation(declaration) is None


def test_contract_accepts_bare_dict_declaration() -> None:
    """插件直接交出方法表字典而不包 ModuleDeclaration 时同样合规。"""
    assert module.module_declaration_violation({"recognize": _handler}) is None


@pytest.mark.parametrize(
    "declaration",
    [
        ModuleDeclaration(methods={}),
        ModuleDeclaration(),
    ],
    ids=["empty_methods", "methods_unset"],
)
def test_contract_rejects_empty_methods(declaration) -> None:
    """方法表为空映射的声明必须被拒绝。"""
    violation = module.module_declaration_violation(declaration)

    assert violation is not None


def test_contract_rejects_non_string_method_name() -> None:
    """方法名不是字符串的声明必须被拒绝。"""
    declaration = ModuleDeclaration(methods={1: _handler})

    violation = module.module_declaration_violation(declaration)

    assert violation is not None


def test_contract_rejects_blank_method_name() -> None:
    """方法名为空白字符串的声明必须被拒绝。"""
    declaration = ModuleDeclaration(methods={"   ": _handler})

    violation = module.module_declaration_violation(declaration)

    assert violation is not None


def test_contract_rejects_non_callable_method_value() -> None:
    """方法名对应值不可调用的声明必须被拒绝。"""
    declaration = ModuleDeclaration(methods={"recognize": "not-callable"})

    violation = module.module_declaration_violation(declaration)

    assert violation is not None


# ---------------------------------------------------------------------------
# PluginProjection.provided_modules() / modules() 投影
# ---------------------------------------------------------------------------


def test_projection_accepts_valid_declaration() -> None:
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableModulePlugin(
        declarations=[
            ModuleDeclaration(methods={"recognize": _handler})
        ]
    )
    projection = PluginProjection({"DemoModule": plugin})

    declared = projection.provided_modules()

    assert len(declared["DemoModule"]) == 1
    accepted = declared["DemoModule"][0]
    assert accepted.methods == {"recognize": _handler}


def test_projection_accepts_bare_dict_without_wrapper() -> None:
    """插件直接交出方法表字典而不包 ModuleDeclaration 的兼容写法应被接受。"""
    plugin = _CapableModulePlugin(declarations=[{"recognize": _handler}])
    projection = PluginProjection({"DemoModule": plugin})

    declared = projection.provided_modules()

    assert declared["DemoModule"] == [{"recognize": _handler}]


def test_projection_partial_rejection_keeps_valid_siblings() -> None:
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableModulePlugin(
        declarations=[
            ModuleDeclaration(methods={"ok": _handler}),
            ModuleDeclaration(methods={}),
            ModuleDeclaration(methods={"bad": "not-callable"}),
        ]
    )
    projection = PluginProjection({"DemoModule": plugin})

    declared = projection.provided_modules()

    assert len(declared["DemoModule"]) == 1
    assert declared["DemoModule"][0].methods == {"ok": _handler}


def test_projection_swallows_plugin_exception_without_blocking_others() -> None:
    """单个插件声明模块方法表抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableModulePlugin(raise_error=True)
    healthy = _CapableModulePlugin(declarations=[ModuleDeclaration(methods={"ok": _handler})])
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_modules()

    assert "Broken" not in declared
    assert declared["Ok"][0].methods == {"ok": _handler}


def test_modules_includes_declared_methods() -> None:
    """声明式方法表须并入 modules() 产出的分发方法表。"""
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"recognize": _handler})]
    )
    projection = PluginProjection({"DemoModule": plugin})

    modules = projection.modules()

    assert modules == {("DemoModule", "模块插件"): {"recognize": _handler}}


def test_modules_merges_declared_and_legacy_sources() -> None:
    """同一实例的声明式方法表与 get_module() 方法表须合并到同一张分发表。"""

    def _legacy() -> str:
        return "legacy"

    plugin = _Plugin(
        provides_modules=lambda: [ModuleDeclaration(methods={"recognize": _handler})],
        get_module=lambda: {"match": _legacy},
    )
    projection = PluginProjection({"Demo": plugin})

    modules = projection.modules()

    assert modules == {("Demo", "测试插件"): {"recognize": _handler, "match": _legacy}}


def test_modules_declared_source_wins_on_name_overlap() -> None:
    """同一实例的两条来源挂载同一方法名时，声明式登记优先生效。"""

    def _legacy_recognize() -> str:
        return "legacy"

    plugin = _Plugin(
        provides_modules=lambda: [ModuleDeclaration(methods={"recognize": _handler})],
        get_module=lambda: {"recognize": _legacy_recognize},
    )
    projection = PluginProjection({"Demo": plugin})

    modules = projection.modules()

    assert modules[("Demo", "测试插件")]["recognize"] is _handler


def test_modules_overlap_warning_fires_once() -> None:
    """两条来源挂载同一方法名时须告警一次，重复投影不重复告警。"""
    errors = []
    log = SimpleNamespace(
        error=lambda message: errors.append(message),
        warning=lambda message: errors.append(message),
    )
    plugin = _Plugin(
        provides_modules=lambda: [ModuleDeclaration(methods={"recognize": _handler})],
        get_module=lambda: {"recognize": lambda: "legacy"},
    )
    projection = PluginProjection({"Demo": plugin}, log=log)

    projection.modules()
    projection.modules()

    overlap_messages = [m for m in errors if "同时挂载方法名" in m]
    assert len(overlap_messages) == 1


def test_modules_legacy_deprecation_warning_still_fires(monkeypatch) -> None:
    """get_module() 一侧的废弃告警不因新增声明式钩子而失效。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin = _Plugin(get_module=lambda: {"recognize": _handler})
    projection = PluginProjection({"Demo": plugin})

    projection.modules()

    assert len(emitted) == 1
    assert "get_module" in emitted[0]


def test_modules_sibling_instance_exception_does_not_block_healthy_instance() -> None:
    """一个实例的模块声明抛异常时，兄弟实例的分发方法表不受影响。"""
    broken = _CapableModulePlugin(raise_error=True)
    healthy = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"ok": _handler})]
    )
    healthy.plugin_name = "健康插件"
    projection = PluginProjection({"Broken": broken, "Healthy": healthy})

    modules = projection.modules()

    assert ("Healthy", "健康插件") in modules
    assert modules[("Healthy", "健康插件")] == {"ok": _handler}
    assert not any(key[0] == "Broken" for key in modules)


# ---------------------------------------------------------------------------
# 三级分发接入
# ---------------------------------------------------------------------------


class _PluginCatalog:
    """把 PluginProjection.modules() 的产出适配为调度器消费的目录端口。"""

    def __init__(self, projection: PluginProjection) -> None:
        """保存被适配的插件能力投影。"""
        self._projection = projection

    def get_plugin_modules(self) -> dict:
        """返回当前插件模块方法表快照。"""
        return self._projection.modules()


def _dispatcher(projection: PluginProjection) -> ModuleInvocationDispatcher:
    """构造只接插件来源、不接宿主模块的最小调度器，用于验证声明式方法表可分发。"""

    class _EmptyModuleCatalog:
        """不提供任何宿主模块的空目录。"""

        def get_running_modules(self, _method: str):
            """始终返回空序列。"""
            return []

        def providers_for(self, _method: str):
            """始终返回空序列。"""
            return ()

    return ModuleInvocationDispatcher(
        module_catalog=_EmptyModuleCatalog(),
        plugin_catalog=_PluginCatalog(projection),
        plugin_error_handler=lambda *a, **k: None,
        system_error_handler=lambda *a, **k: None,
        rate_limit_handler=lambda *a, **k: None,
    )


def test_declared_module_method_participates_in_unicast_dispatch() -> None:
    """provides_modules() 声明的方法须能被单播分发触达并取得返回值。"""
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"recognize": lambda: "declared"})]
    )
    projection = PluginProjection({"DemoModule": plugin})
    dispatcher = _dispatcher(projection)

    assert dispatcher.unicast("recognize") == "declared"


def test_declared_module_method_participates_in_multicast_dispatch() -> None:
    """provides_modules() 声明的方法须能被多播分发收集到结果列表中。"""
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"recognize": lambda: "declared"})]
    )
    projection = PluginProjection({"DemoModule": plugin})
    dispatcher = _dispatcher(projection)

    assert dispatcher.multicast("recognize") == ["declared"]


def test_declared_module_method_participates_in_broadcast_dispatch() -> None:
    """provides_modules() 声明的方法须能被广播分发触达。"""
    calls = []
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"notify": lambda: calls.append(1)})]
    )
    projection = PluginProjection({"DemoModule": plugin})
    dispatcher = _dispatcher(projection)

    dispatcher.broadcast("notify")

    assert calls == [1]


def test_contract_invalid_declaration_never_reaches_dispatch() -> None:
    """契约不合规的声明被投影层拒绝，不会作为分发方法表的一部分被调用方触达。"""
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"recognize": "not-callable"})]
    )
    projection = PluginProjection({"DemoModule": plugin})
    dispatcher = _dispatcher(projection)

    assert dispatcher.unicast("recognize") is None


def test_provider_source_yields_declared_provider_for_matching_method() -> None:
    """PluginProviderSource 须按方法名精确产出声明式方法表登记的提供者。"""
    plugin = _CapableModulePlugin(
        declarations=[ModuleDeclaration(methods={"recognize": _handler})]
    )
    projection = PluginProjection({"DemoModule": plugin})
    source = PluginProviderSource(_PluginCatalog(projection))

    providers = list(source.notify_providers("recognize"))

    assert len(providers) == 1
    assert providers[0].extension_id == "DemoModule"
    assert providers[0].invoke() == "ok"
