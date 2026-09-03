"""统一分类领域与具体媒体来源之间的架构边界测试。"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION_ROOTS = (
    PROJECT_ROOT / "app" / "domain" / "classification",
    PROJECT_ROOT / "app" / "application" / "classification",
)


def _imported_modules(path: Path) -> tuple[str, ...]:
    """静态提取一个分类模块中的绝对导入目标。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


def test_classification_layers_do_not_import_concrete_source_modules() -> None:
    """纯规则和应用服务不得反向依赖 TMDB、音乐或插件来源实现。"""
    violations = [
        (path.relative_to(PROJECT_ROOT).as_posix(), module)
        for root in CLASSIFICATION_ROOTS
        for path in sorted(root.rglob("*.py"))
        for module in _imported_modules(path)
        if module == "app.modules" or module.startswith("app.modules.")
    ]

    assert violations == []
