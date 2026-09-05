"""MediaChain 稳定插件 ABI 与同名包结构门禁。"""

import ast
import importlib
import inspect
import pickle
import subprocess
import sys
from pathlib import Path

from app.chain.base import ChainBase
from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.foundation.singleton import Singleton

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEDIA_PACKAGE = PROJECT_ROOT / "app" / "chain" / "media"


def _parameter_contract(method: object) -> tuple[tuple[str, object], ...]:
    """提取插件 ABI 关心的参数顺序和默认值。"""
    return tuple((name, parameter.default) for name, parameter in inspect.signature(method).parameters.items())


def test_media_package_exposes_only_stable_chain_facade() -> None:
    """包根只惰性暴露稳定 MediaChain，内部 owner 不得重复导出。"""
    module = importlib.import_module("app.chain.media")

    assert module.__all__ == ["MediaChain"]
    assert module.MediaChain is MediaChain
    assert not {
        "MediaCatalogOwner",
        "MediaMusicOwner",
        "MediaProjectionOwner",
        "MediaRecognitionOwner",
        "MediaSearchOwner",
    }.intersection(vars(module))


def test_media_package_root_keeps_facade_lazy_in_clean_interpreter() -> None:
    """首次导入包根不得提前加载门面及其全部 owner 依赖。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.chain.media as module; "
                "assert module.__all__ == ['MediaChain']; "
                "assert 'app.chain.media.facade' not in sys.modules"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_media_chain_preserves_class_identity_mro_and_pickle_contract() -> None:
    """拆包不得改变插件动态导入、Singleton、MRO 或 pickle 类身份。"""
    assert MediaChain.__module__ == "app.chain.media"
    assert MediaChain.__qualname__ == "MediaChain"
    assert MediaChain.__mro__ == (MediaChain, *ChainBase.__mro__)
    assert type(MediaChain) is Singleton
    assert pickle.loads(pickle.dumps(MediaChain)) is MediaChain
    instance = object.__new__(MediaChain)
    restored = pickle.loads(pickle.dumps(instance))
    assert type(restored) is MediaChain
    assert vars(restored) == {}


def test_media_chain_preserves_shared_facade_state_contract() -> None:
    """跨 owner 共享状态必须仍由唯一稳定门面集中持有。"""
    assert isinstance(MediaChain._album_dir_cache, AlbumDirectoryCache)
    assert MediaChain._album_dir_cache_max == 128
    assert MediaChain._album_match_min_files == 2


def test_media_chain_preserves_official_plugin_method_contracts() -> None:
    """锁定官方插件真实调用的同步、异步方法参数合同。"""
    assert _parameter_contract(MediaChain.recognize_media) == (
        ("self", inspect.Parameter.empty),
        ("meta", None),
        ("mtype", None),
        ("media_source", None),
        ("media_id", None),
        ("episode_group", None),
        ("cache", True),
        ("share_meta", None),
        ("music_type", None),
    )
    assert _parameter_contract(MediaChain.search) == (
        ("self", inspect.Parameter.empty),
        ("title", inspect.Parameter.empty),
        ("media_source", None),
        ("mtype", None),
        ("limit", 20),
    )
    assert _parameter_contract(MediaChain.async_search) == _parameter_contract(MediaChain.search)
    assert inspect.iscoroutinefunction(MediaChain.async_search)
    assert _parameter_contract(MediaChain.search_medias) == (
        ("self", inspect.Parameter.empty),
        ("meta", inspect.Parameter.empty),
        ("media_source", None),
    )
    assert _parameter_contract(MediaChain.convert_media_identity) == (
        ("self", inspect.Parameter.empty),
        ("target_source", inspect.Parameter.empty),
        ("media_source", inspect.Parameter.empty),
        ("media_id", inspect.Parameter.empty),
        ("mtype", None),
        ("season", None),
    )


def test_media_chain_uses_named_owner_package_without_legacy_source() -> None:
    """媒体能力使用单词职责文件，旧平铺单体不得复活。"""
    assert not (PROJECT_ROOT / "app" / "chain" / "media.py").exists()
    assert {path.name for path in MEDIA_PACKAGE.glob("*.py")} == {
        "__init__.py",
        "album.py",
        "auxiliary.py",
        "cache.py",
        "catalog.py",
        "contract.py",
        "facade.py",
        "path.py",
        "plugin.py",
        "projection.py",
        "recognition.py",
        "search.py",
    }


def test_media_owner_modules_do_not_depend_on_package_root_or_facade() -> None:
    """owner 只通过静态合同协作，不得反向导入包根或稳定门面。"""
    forbidden = {"app.chain.media", "app.chain.media.facade"}
    violations = {}
    for path in MEDIA_PACKAGE.glob("*.py"):
        if path.name in {"__init__.py", "facade.py"}:
            continue
        imported = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module)
                elif node.level == 1:
                    base = "app.chain.media"
                    if node.module:
                        imported.add(f"{base}.{node.module}")
                    else:
                        imported.update(f"{base}.{alias.name}" for alias in node.names)
        if owners := imported.intersection(forbidden):
            violations[path.name] = sorted(owners)

    assert not violations
