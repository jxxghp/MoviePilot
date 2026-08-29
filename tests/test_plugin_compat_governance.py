import ast
import importlib
import json
import re
from pathlib import Path

from app.runtime.compat.manifest import MODULE_ALIASES, PACKAGE_ALIASES

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
OFFICIAL_PLUGIN_BASELINE = (
    PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "official-plugin-baseline.json"
)
FILENAME_POLICY = PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "filename-policy.json"
PLUGIN_COMPONENT_PACKAGES = (
    APP_ROOT / "adapters" / "external" / "plugin",
    APP_ROOT / "adapters" / "system" / "plugin",
    APP_ROOT / "application" / "plugin",
    APP_ROOT / "runtime" / "extensions" / "plugin",
)
AUDITED_PLUGIN_MANAGER_MEMBERS = {
    "get_plugin_config",
    "get_plugin_ids",
    "plugins",
    "running_plugins",
}
DELEGATED_PLUGIN_CATALOG_METHODS = {
    "async_get_online_plugin_candidates",
    "async_get_online_plugins",
    "async_get_plugins_from_market",
    "get_installed_plugins",
    "get_local_plugin_version",
    "get_local_plugins",
    "get_local_repo_plugins",
    "get_online_plugins",
    "get_plugins_from_market",
    "is_plugin_exists",
    "process_plugins_list",
}


def _parse(path: Path) -> ast.Module:
    """解析 UTF-8 Python 源码，兼容历史插件文件可能携带的 BOM。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _module_imports(tree: ast.AST) -> set[str]:
    """返回语法树中的绝对导入模块，不把导入符号误当成子模块。"""
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module)
    return imports


def _public_names(tree: ast.AST) -> set[str]:
    """返回模块定义或非私有导入到顶层的符号名。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return {name for name in names if not name.startswith("_")}


def test_official_plugin_legacy_imports_are_exactly_routed_or_retired() -> None:
    """官方插件旧导入必须有精确 Compat 路由，或保持一条已审查的可选退役路径。"""
    baseline = json.loads(OFFICIAL_PLUGIN_BASELINE.read_text(encoding="utf-8"))
    legacy_imports = {
        module
        for module in baseline["imports"]
        if module == "app.log"
        or module.startswith(("app.core.", "app.helper.", "app.utils."))
    }
    routed = set(MODULE_ALIASES) | set(PACKAGE_ALIASES)

    # V2 AgentResourceOfficer 对该模块使用可选导入；宿主已明确删除，不应为它恢复源码或猜测映射。
    explicitly_retired = {"app.helper.subscribe"}
    assert legacy_imports - routed == explicitly_retired
    assert explicitly_retired.isdisjoint(routed)


def test_plugin_manager_legacy_and_sdk_paths_share_canonical_identity() -> None:
    """V2 旧路径与 V3 SDK 必须复用同一个 PluginManager 身份及已验证成员。"""
    canonical = importlib.import_module("app.runtime.extensions.plugin_manager")
    legacy = importlib.import_module("app.core.plugin")
    sdk = importlib.import_module("app.sdk.plugins")

    assert MODULE_ALIASES["app.core.plugin"].target == canonical.__name__
    assert MODULE_ALIASES["app.core.plugin"].replacement == "app.sdk.plugins"
    assert legacy is canonical
    assert legacy.PluginManager is canonical.PluginManager
    assert sdk.PluginManager is canonical.PluginManager
    assert AUDITED_PLUGIN_MANAGER_MEMBERS <= set(dir(canonical.PluginManager))
    assert sdk.__all__ == ["ModuleManager", "PluginManager"]
    assert not hasattr(sdk, "PluginHelper")


def test_plugin_helper_stays_on_exact_legacy_adapter_route() -> None:
    """PluginHelper 只保留精确旧 Helper 路由，不进入推荐插件 SDK。"""
    canonical = importlib.import_module("app.adapters.external.market")
    legacy = importlib.import_module("app.helper.plugin")
    alias = MODULE_ALIASES["app.helper.plugin"]

    assert alias.target == canonical.__name__
    assert alias.replacement == canonical.__name__
    assert legacy is canonical
    assert legacy.PluginHelper is canonical.PluginHelper


def test_plugin_entry_and_application_do_not_reimport_concrete_legacy_owners() -> None:
    """Plugin 入口与应用用例不得重新依赖 PluginHelper、ServerHelper 或具体 PluginManager。"""
    forbidden_modules = {
        "app.adapters.external.market",
        "app.adapters.external.server",
        "app.runtime.extensions.plugin_manager",
    }
    forbidden_names = {"MoviePilotServerHelper", "PluginHelper", "PluginManager"}
    paths = [APP_ROOT / "api" / "endpoints" / "plugin.py"]
    paths.extend((APP_ROOT / "application" / "plugin").rglob("*.py"))
    violations: list[str] = []

    for path in paths:
        tree = _parse(path)
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        violations.extend(
            f"{relative}:import:{module}"
            for module in sorted(_module_imports(tree) & forbidden_modules)
        )
        referenced = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in forbidden_names
        }
        violations.extend(f"{relative}:name:{name}" for name in sorted(referenced))

    assert violations == []


def test_plugin_manager_catalog_compat_methods_remain_thin_delegates() -> None:
    """PluginManager 的旧市场目录方法只能委托，不得重新吸收应用目录算法。"""
    path = APP_ROOT / "runtime" / "extensions" / "plugin_manager.py"
    tree = _parse(path)
    manager = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PluginManager"
    )
    methods = {
        node.name: node
        for node in manager.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in DELEGATED_PLUGIN_CATALOG_METHODS
    }

    assert set(methods) == DELEGATED_PLUGIN_CATALOG_METHODS
    for method_name, method in methods.items():
        statements = [
            statement
            for statement in method.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(statements) == 1, method_name
        assert isinstance(statements[0], ast.Return), method_name
        value = statements[0].value
        if isinstance(value, ast.Await):
            value = value.value
        assert isinstance(value, ast.Call), method_name
        assert ast.unparse(value.func).startswith("self._plugin_catalog_view."), method_name


def test_plugin_capability_package_roots_do_not_duplicate_exports() -> None:
    """多文件插件能力包根只保留说明，不得复制子模块实现或宿主导出。"""
    violations: list[str] = []
    for package in PLUGIN_COMPONENT_PACKAGES:
        path = package / "__init__.py"
        tree = _parse(path)
        if not ast.get_docstring(tree) or len(tree.body) != 1 or _public_names(tree):
            violations.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert violations == []


def test_new_host_production_modules_use_single_word_filenames() -> None:
    """宿主多词生产文件必须等于审查低水位，新增文件只能使用单个小写词。"""
    policy = json.loads(FILENAME_POLICY.read_text(encoding="utf-8"))
    assert policy["schema_version"] == 1
    assert policy["scope"] == {
        "root": "app",
        "excluded": ["app/plugins"],
        "special": ["__init__.py"],
    }
    single_word = re.compile(r"[a-z][a-z0-9]*")
    actual = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if not path.is_relative_to(APP_ROOT / "plugins")
        and path.name != "__init__.py"
        and not single_word.fullmatch(path.stem)
    )

    assert actual == policy["grandfathered_multiword_files"]
