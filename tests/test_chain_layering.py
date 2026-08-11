"""处理链模块分层约束测试。"""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAIN_ROOT = PROJECT_ROOT / "app" / "chain"


def _imported_modules(path: Path) -> set[str]:
    """解析源码中的导入模块，包含函数内部的延迟导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _inherited_recognize_calls(path: Path) -> list[tuple[int, str]]:
    """查找业务链通过 self 隐式调用媒体识别入口的位置。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
                isinstance(owner, ast.Name)
                and owner.id == "self"
                and node.func.attr in {"recognize_media", "async_recognize_media"}
        ):
            calls.append((node.lineno, node.func.attr))
    return calls


def test_chain_base_does_not_import_concrete_chains() -> None:
    """基础链不得反向导入任何具体处理链。"""
    imports = _imported_modules(CHAIN_ROOT / "__init__.py")

    assert not {
        module for module in imports
        if module.startswith("app.chain.")
    }


def test_music_chain_only_depends_on_chain_base() -> None:
    """音乐领域链不得反向依赖媒体编排链或其他业务链。"""
    imports = _imported_modules(CHAIN_ROOT / "music.py")

    assert not {
        module for module in imports
        if module.startswith("app.chain.")
    }


def test_business_chains_delegate_recognition_to_media_chain() -> None:
    """搜索、订阅、下载和转移链必须显式委托媒体识别编排层。"""
    violations = {
        name: calls
        for name in ("search.py", "subscribe.py", "download.py", "transfer.py")
        if (calls := _inherited_recognize_calls(CHAIN_ROOT / name))
    }

    assert not violations
