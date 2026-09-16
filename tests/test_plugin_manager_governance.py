"""PluginManager 类型化 Runtime 与职责边界门禁。"""

import ast
from pathlib import Path

from app.startup.initializers import plugins as plugins_initializer

PROJECT_ROOT = Path(__file__).parents[1]
MANAGER_PATH = PROJECT_ROOT / "app" / "runtime" / "extensions" / "plugin" / "manager.py"
RUNTIME_PATH = PROJECT_ROOT / "app" / "runtime" / "extensions" / "plugin" / "runtime.py"


def _parse(path: Path) -> ast.Module:
    """解析治理门禁消费的 Python 模块。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _manager_class() -> ast.ClassDef:
    """返回 canonical PluginManager 类节点。"""
    return next(
        node
        for node in _parse(MANAGER_PATH).body
        if isinstance(node, ast.ClassDef) and node.name == "PluginManager"
    )


def test_plugin_manager_only_consumes_injected_runtime_factory() -> None:
    """Manager 构造器只能消费注入工厂，不得构造 Runtime 或任一职责 owner。"""
    manager = _manager_class()
    initializer = next(
        node
        for node in manager.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(initializer)
        if isinstance(node, ast.Call)
    ]
    manager_calls = {
        ast.unparse(node.func)
        for node in ast.walk(manager)
        if isinstance(node, ast.Call)
    }
    forbidden = {
        "PluginAccessPolicy",
        "PluginCatalogFacade",
        "PluginCloneService",
        "PluginDependencyService",
        "PluginLifecycle",
        "PluginLoader",
        "PluginMetadataMapper",
        "PluginMonitorController",
        "PluginPathResolver",
        "PluginProjection",
        "PluginRegistry",
        "PluginSyncService",
        "PluginToolCatalog",
    }

    assert calls.count("_plugin_runtime_factory") == 1
    assert "build_plugin_runtime" not in calls
    assert "PluginRuntimeEnvironment" not in calls
    assert forbidden.isdisjoint(manager_calls)


def test_plugin_runtime_dependency_graph_lives_in_startup_composition() -> None:
    """Runtime Environment 与 builder 只能由启动组合根连接。"""
    startup_path = PROJECT_ROOT / "app" / "startup" / "initializers" / "plugins.py"
    manager_imports = {
        node.name
        for node in ast.walk(_parse(MANAGER_PATH))
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.runtime.extensions.plugin.runtime"
        for node in node.names
    }
    startup_calls = {
        ast.unparse(node.func)
        for node in ast.walk(_parse(startup_path))
        if isinstance(node, ast.Call)
    }

    assert manager_imports == {"PluginRuntime"}
    assert {
        "PluginRuntimeEnvironment",
        "build_plugin_runtime",
        "configure_plugin_runtime_factory",
    } <= startup_calls


def test_startup_injects_runtime_factory_before_manager_materialization() -> None:
    """所有启动入口必须先发布 Runtime 工厂，再物化 PluginManager。"""
    startup_path = PROJECT_ROOT / "app" / "startup" / "initializers" / "plugins.py"
    functions = {
        node.name: node
        for node in _parse(startup_path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    runtime_calls = sorted(
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(functions["configure_plugin_runtime_services"])
        if isinstance(node, ast.Call)
    )
    sync_calls = sorted(
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(functions["sync_plugins"])
        if isinstance(node, ast.Call)
    )

    assert runtime_calls.index(
        next(item for item in runtime_calls if item[1] == "configure_plugin_runtime_factory")
    ) < runtime_calls.index(
        next(item for item in runtime_calls if item[1] == "configure_plugin_runtime")
    )
    assert not any(item[1] == "configure_plugin_services" for item in sync_calls)


def test_plugin_runtime_services_publish_application_runtime(monkeypatch) -> None:
    """模块对象图构造前必须先发布 Runtime 工厂和 Application provider。"""
    order: list[str] = []
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_catalog_factory",
        lambda _factory: order.append("catalog"),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_runtime_factory",
        lambda _factory: order.append("factory"),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_runtime",
        lambda _provider, **_kwargs: order.append("provider"),
    )

    plugins_initializer.configure_plugin_runtime_services()

    assert order == ["catalog", "factory", "provider"]


def test_plugin_manager_does_not_reach_raw_system_adapters() -> None:
    """兼容门面不得穿透市场、包或依赖适配器的原始属性。"""
    tree = _parse(MANAGER_PATH)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    raw_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"market", "package", "dependency"}
    }

    assert not any(module.startswith("app.adapters") for module in imported_modules)
    assert raw_attributes == set()


def test_plugin_runtime_is_the_only_owner_aggregate() -> None:
    """类型化 Runtime 必须集中列出唯一 owner，且包根不提供重复导出。"""
    tree = _parse(RUNTIME_PATH)
    runtime = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PluginRuntime"
    )
    fields = {
        node.target.id
        for node in runtime.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    package_root = RUNTIME_PATH.parent / "__init__.py"

    assert {
        "catalog",
        "dependencies",
        "lifecycle",
        "loader",
        "monitor",
        "projection",
        "registry",
        "sync",
        "system",
    } <= fields
    assert len(_parse(package_root).body) == 1


def test_dependency_classification_lives_in_dependency_owner() -> None:
    """虚拟实例分类和状态写入不得回流 PluginManager。"""
    manager = _manager_class()
    methods = {
        node.name: node
        for node in manager.body
        if isinstance(node, ast.FunctionDef)
    }
    for name, delegate in (
        ("classify_plugins", "self._plugin_dependencies.classify_plugins"),
        (
            "apply_plugin_dependency_classification",
            "self._plugin_dependencies.apply_classification",
        ),
    ):
        calls = [
            ast.unparse(node.func)
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Call)
        ]
        assert calls == [delegate]
