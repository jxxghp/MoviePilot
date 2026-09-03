"""DownloadChain 同名包职责、组合方式与宿主导入边界门禁。"""

import ast
from pathlib import Path

from app.chain.base import ChainBase

# 包根通过 __getattr__ 惰性保留旧导入，Pylint 无法静态解析该符号。
from app.chain.download import DownloadChain  # pylint: disable=no-name-in-module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"
TEST_ROOT = PROJECT_ROOT / "tests"
CHAIN_ROOT = APP_ROOT / "chain"
DOWNLOAD_PACKAGE = CHAIN_ROOT / "download"


def _tree(path: Path) -> ast.Module:
    """解析指定 Python 文件。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _class_methods(path: Path, class_name: str) -> set[str]:
    """返回指定类直接定义的方法名。"""
    class_node = next(
        node
        for node in _tree(path).body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_download_monolith_is_retired_and_package_owners_are_complete() -> None:
    """旧单体不得复活，下载职责必须完整收口到同名包。"""
    assert not (CHAIN_ROOT / "download.py").exists()
    assert {path.name for path in DOWNLOAD_PACKAGE.glob("*.py")} == {
        "__init__.py",
        "batch.py",
        "contract.py",
        "existence.py",
        "facade.py",
        "failure.py",
        "history.py",
        "ports.py",
        "processing.py",
        "selection.py",
        "submission.py",
        "subtitle.py",
        "tasks.py",
    }


def test_download_package_root_only_declares_lazy_stable_export() -> None:
    """包根只能惰性解析 DownloadChain，不得复制实现或内部端口。"""
    package_tree = _tree(DOWNLOAD_PACKAGE / "__init__.py")
    app_imports = [
        node
        for node in package_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.")
            or isinstance(node, ast.Import)
            and any(alias.name.startswith("app.") for alias in node.names)
        )
    ]
    classes = [node.name for node in package_tree.body if isinstance(node, ast.ClassDef)]

    assert app_imports == []
    assert classes == []
    assert DownloadChain.__module__ == "app.chain.download"
    assert __import__("app.chain.download", fromlist=["__all__"]).__all__ == [
        "DownloadChain"
    ]


def test_download_facade_is_thin_and_has_one_chain_base() -> None:
    """Facade 只组合 owner 和稳定事件代理，继承图只有一个 ChainBase。"""
    facade_path = DOWNLOAD_PACKAGE / "facade.py"
    facade_tree = _tree(facade_path)
    facade_class = next(
        node
        for node in facade_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DownloadChain"
    )
    event_proxy = next(
        node
        for node in facade_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "download_file_deleted"
    )

    assert len(facade_path.read_text(encoding="utf-8").splitlines()) <= 60
    assert _class_methods(facade_path, "DownloadChain") == {
        "download_file_deleted"
    }
    assert [ast.unparse(decorator) for decorator in event_proxy.decorator_list] == [
        "eventmanager.register(EventType.DownloadFileDeleted)",
        "_public_handler",
    ]
    assert [
        ast.unparse(statement)
        for statement in event_proxy.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ] == ["self._download_file_deleted(event)"]
    assert DownloadChain.__mro__.count(ChainBase) == 1
    assert DownloadChain.__mro__[-5:] == (
        ChainBase,
        *ChainBase.__mro__[1:],
    )


def test_download_public_workflows_have_unique_owners() -> None:
    """公开下载流程必须各由一个 owner 定义，避免 MRO 遮蔽重复实现。"""
    expected_owners = {
        "batch_download": "batch.py",
        "download_single": "submission.py",
        "get_no_exists_info": "existence.py",
    }
    actual_owners: dict[str, list[str]] = {name: [] for name in expected_owners}
    for path in DOWNLOAD_PACKAGE.glob("*.py"):
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in actual_owners:
                    actual_owners[node.name].append(path.name)

    assert actual_owners == {
        name: [owner] for name, owner in expected_owners.items()
    }


def test_host_does_not_import_download_internals_from_package_root() -> None:
    """宿主和测试可使用稳定类型，但不得从包根取得内部实现。"""
    violations: list[str] = []
    for source_root in (APP_ROOT, TEST_ROOT):
        for path in source_root.rglob("*.py"):
            if "plugins" in path.parts or path == DOWNLOAD_PACKAGE / "__init__.py":
                continue
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module != "app.chain.download":
                    continue
                forbidden = sorted(
                    alias.name for alias in node.names if alias.name != "DownloadChain"
                )
                if forbidden:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:"
                        f"{','.join(forbidden)}"
                    )

    assert violations == []
