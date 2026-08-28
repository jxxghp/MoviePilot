"""ChainBase canonical、冷导入与旧插件符号边界。"""

import ast
import itertools
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_chain_package_root_has_no_canonical_implementation_or_export() -> None:
    """物理包根只能声明包用途，不得重新承载或导出 ChainBase。"""
    path = PROJECT_ROOT / "app" / "chain" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert all(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for node in tree.body
    )


def test_host_python_sources_do_not_import_chainbase_from_package_root() -> None:
    """排除插件副本后，宿主和测试必须统一使用 canonical 子模块。"""
    violations: list[str] = []
    for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path.is_relative_to(PROJECT_ROOT / "app" / "plugins"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "app.chain":
                    continue
                if any(alias.name == "ChainBase" for alias in node.names):
                    violations.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert violations == []


def test_chain_modules_are_cold_importable_in_every_order() -> None:
    """base 与两个 mixin 的任意导入顺序都不得依赖部分初始化包。"""
    modules = ("app.chain.base", "app.chain._messaging", "app.chain._recognition")
    for order in itertools.permutations(modules):
        code = (
            "import importlib, sys; "
            f"order={order!r}; "
            "[importlib.import_module(name) for name in order]; "
            "package=sys.modules['app.chain']; "
            "assert 'ChainBase' not in vars(package)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_chain_package_cold_import_does_not_load_children() -> None:
    """只导入物理包根时不得急切加载 Chain 实现或 mixin。"""
    code = (
        "import sys; import app.chain; "
        "loaded=sorted(name for name in sys.modules if name.startswith('app.chain.')); "
        "assert loaded == [], loaded"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
