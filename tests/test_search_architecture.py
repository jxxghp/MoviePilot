"""搜索 Chain 包化后的静态边界与稳定继承结构回归测试。"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.chain._recognition import RecognitionMixin
from app.chain.base import ChainBase
from app.chain.search import SearchChain

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEARCH_PACKAGE = PROJECT_ROOT / "app" / "chain" / "search"
EXPECTED_SEARCH_MODULES = {
    "__init__.py",
    "cache.py",
    "contract.py",
    "execution.py",
    "facade.py",
    "media.py",
    "music.py",
    "pagination.py",
    "plan.py",
    "provider.py",
    "recommend.py",
    "result.py",
    "site.py",
    "subtitle.py",
    "title.py",
}


def test_search_chain_is_a_single_named_package_without_legacy_module():
    """同名包必须完整取代旧单文件，且子模块名维持单词职责名。"""
    assert SEARCH_PACKAGE.is_dir()
    assert not (SEARCH_PACKAGE.parent / "search.py").exists()
    assert {path.name for path in SEARCH_PACKAGE.glob("*.py")} == EXPECTED_SEARCH_MODULES
    assert not (SEARCH_PACKAGE / "source.py").exists()


def test_search_package_root_lazily_exports_only_search_chain(tmp_path):
    """隔离进程验证包根不会因导入而提前装载门面和所有 owner。"""
    script = """
import sys
from app.testing.bootstrap import prepare_backend

prepare_backend()
import app.chain.search as search

assert search.__all__ == ["SearchChain"]
assert set(search._EXPORTS) == {"SearchChain"}
assert "SearchChain" not in vars(search)
assert "app.chain.search.facade" not in sys.modules
assert search.SearchChain.__module__ == "app.chain.search"
assert "app.chain.search.facade" in sys.modules
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["CONFIG_DIR"] = str(tmp_path / "config")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_search_owners_do_not_import_package_root_or_facade():
    """owner 只能依赖明确子模块，禁止反向依赖公开包根或门面。"""
    forbidden_modules = {"app.chain.search", "app.chain.search.facade"}
    violations: list[str] = []
    for path in sorted(SEARCH_PACKAGE.glob("*.py")):
        if path.name in {"__init__.py", "facade.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module in forbidden_modules or (node.level and node.module in {None, "facade"})
            ):
                target = "." * node.level + (node.module or "")
                violations.append(f"{path.name}:{node.lineno}:{target}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        violations.append(f"{path.name}:{node.lineno}:{alias.name}")
    assert violations == []


def test_search_facade_directly_extends_chain_base_with_stable_mro():
    """插件依赖的直接父类和完整 MRO 不得因职责拆分发生漂移。"""
    assert SearchChain.__bases__ == (ChainBase,)
    assert SearchChain.__mro__ == (
        SearchChain,
        ChainBase,
        RecognitionMixin,
        MessageProcessingMixin,
        NotificationMixin,
        object,
    )
