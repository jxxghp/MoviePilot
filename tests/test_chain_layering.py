"""处理链模块分层约束测试。"""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAIN_ROOT = PROJECT_ROOT / "app" / "chain"
LEGACY_MUSIC_SCAN_ROOTS = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "scripts",
)
MUSIC_SOURCE_CHAIN_FILES = (
    "acoustid.py",
    "douban.py",
    "listenbrainz.py",
    "musicbrainz.py",
    "theaudiodb.py",
)


def _imported_modules(path: Path) -> set[str]:
    """解析源码中的导入模块，包含函数内部的延迟导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
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

    # 下划线前缀的内部模块（_messaging/_recognition 等）是 ChainBase 的
    # 功能域 mixin，不是具体处理链，允许导入
    assert not {
        module
        for module in imports
        if module.startswith("app.chain.")
        and not module.removeprefix("app.chain.").startswith("_")
    }


def test_legacy_music_chain_is_removed() -> None:
    """聚合全部音乐职责的旧 MusicChain 文件和导入不得重新出现。"""
    assert not (CHAIN_ROOT / "music.py").exists()
    violations = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            module for module in _imported_modules(path) if module == "app.chain.music"
        )
        for root in LEGACY_MUSIC_SCAN_ROOTS
        for path in root.rglob("*.py")
        if "plugins" not in path.parts  # 插件目录由插件仓自治，跳过
        if "app.chain.music" in _imported_modules(path)
    }
    assert not violations


def test_music_source_chains_do_not_depend_on_public_orchestration_chains() -> None:
    """音乐数据源链不得反向依赖识别、刮削、搜索或推荐编排链。"""
    forbidden = {
        "app.chain.media",
        "app.chain.recommend",
        "app.chain.scraping",
        "app.chain.search",
    }
    violations = {
        filename: sorted(_imported_modules(CHAIN_ROOT / filename).intersection(forbidden))
        for filename in MUSIC_SOURCE_CHAIN_FILES
        if _imported_modules(CHAIN_ROOT / filename).intersection(forbidden)
    }
    assert not violations


def test_media_chain_excludes_scraping_and_music_exploration_methods() -> None:
    """MediaChain 只保留公共识别与详情路由，不得重新承接刮削或音乐探索职责。"""
    path = CHAIN_ROOT / "media.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forbidden_methods = {
        "async_get_doubaninfo_by_bangumiid",
        "async_get_doubaninfo_by_tmdbid",
        "async_get_tmdbinfo_by_bangumiid",
        "async_get_tmdbinfo_by_doubanid",
        "scrape_metadata",
        "scrape_metadata_event",
        "scrape_music_metadata",
        "get_doubaninfo_by_bangumiid",
        "get_doubaninfo_by_tmdbid",
        "get_music_lyrics",
        "get_tmdbinfo_by_bangumiid",
        "get_tmdbinfo_by_doubanid",
        "async_get_music_lyrics",
        "music_chart",
        "async_music_chart",
        "music_discover",
        "async_music_discover",
        "async_music_fresh_releases",
    }

    assert not method_names.intersection(forbidden_methods)
    assert "app.chain.scraping" not in _imported_modules(path)


def test_business_chains_delegate_recognition_to_media_chain() -> None:
    """搜索、订阅、下载和转移链必须显式委托媒体识别编排层。"""
    violations = {
        name: calls
        for name in ("search.py", "subscribe.py", "download.py", "transfer.py")
        if (calls := _inherited_recognize_calls(CHAIN_ROOT / name))
    }

    assert not violations
