"""Scheduler 同名包职责与兼容边界门禁。"""

import ast
from pathlib import Path

from app.scheduler import Scheduler, SchedulerChain  # pylint: disable=no-name-in-module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"
SCHEDULER_ROOT = APP_ROOT / "scheduler"


def _tree(path: Path) -> ast.Module:
    """解析指定 Python 文件。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def test_scheduler_monolith_is_retired_and_package_owners_exist() -> None:
    """旧单体必须消失，路线图声明的职责 owner 必须各有文件。"""
    assert not (APP_ROOT / "scheduler.py").exists()
    assert {
        "bridge.py",
        "catalog.py",
        "execution.py",
        "facade.py",
        "lifecycle.py",
        "progress.py",
        "reconcile.py",
        "registry.py",
        "services.py",
    } <= {path.name for path in SCHEDULER_ROOT.glob("*.py")}


def test_scheduler_package_root_is_only_stable_legacy_abi() -> None:
    """包根只延迟公开迁移前的两个类型，不重复承载实现。"""
    package_tree = _tree(SCHEDULER_ROOT / "__init__.py")
    classes = [node.name for node in package_tree.body if isinstance(node, ast.ClassDef)]

    assert classes == []
    assert Scheduler.__module__ == "app.scheduler"
    assert SchedulerChain.__module__ == "app.scheduler"


def test_scheduler_facade_remains_thin_and_composes_named_owners() -> None:
    """Facade 只组合职责 owner，不重新吸收业务实现。"""
    facade_path = SCHEDULER_ROOT / "facade.py"
    facade_tree = _tree(facade_path)
    scheduler_class = next(
        node
        for node in facade_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Scheduler"
    )
    methods = {
        node.name
        for node in scheduler_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert len(facade_path.read_text(encoding="utf-8").splitlines()) <= 160
    assert methods == {
        "__init__",
        "_scheduler_services",
        "configure_services",
        "get_reload_name",
        "on_plugin_reload",
        "reset_runtime_bindings",
    }


def test_scheduler_package_does_not_construct_chains() -> None:
    """业务 Chain 只能由 startup 组合根构造后注入 Scheduler。"""
    violations: list[str] = []
    for path in SCHEDULER_ROOT.glob("*.py"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id.endswith("Chain"):
                violations.append(f"{path.name}:{node.lineno}:{node.func.id}")

    assert violations == []


def test_host_code_does_not_import_scheduler_plugin_abi_root() -> None:
    """除兼容包自身外，宿主只能使用 application 门面或 concrete 子模块。"""
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "plugins" in path.parts or path == SCHEDULER_ROOT / "__init__.py":
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.scheduler":
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.scheduler":
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                        )

    assert violations == []
