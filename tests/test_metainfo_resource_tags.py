"""
验证资源规格方括号不会单独把电影名称路由到动漫解析器。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.adapters.system import rust as rust_accel
from app.domain import metainfo as metainfo_module
from app.domain.meta import runtime as meta_runtime
from app.domain.metainfo import MetaInfo, MetaInfoPath, is_anime
from app.schemas.types import MediaType


@pytest.fixture(params=[False, True], ids=["python", "rust"])
def recognition_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """
    分别运行 Python 回退与真实 Rust 扩展，结束后恢复识别运行时和选项缓存。
    """
    if request.param:
        assert rust_accel.is_available(), "Rust 回归必须运行真实扩展"
    monkeypatch.setattr(rust_accel, "is_enabled", lambda: True)
    # 精确恢复原始 provider，避免把依赖配置的回调替换成一次读取的固定快照。
    monkeypatch.setattr(meta_runtime, "_media_extensions_provider", lambda: (".mkv",))
    monkeypatch.setattr(meta_runtime, "_audio_extensions_provider", lambda: ())
    monkeypatch.setattr(meta_runtime, "_metainfo_accelerator", rust_accel if request.param else None)
    metainfo_module.clear_rust_parse_options_cache()
    try:
        yield
    finally:
        metainfo_module.clear_rust_parse_options_cache()


@pytest.mark.usefixtures("recognition_backend")
@pytest.mark.parametrize("brackets", [("[", "]"), ("【", "】")])
@pytest.mark.parametrize("separator", ["", "-"])
@pytest.mark.parametrize("resolution", ["720p", "1080p", "1080i", "2160P", "1080I"])
def test_resource_brackets_preserve_movie_year(
    brackets: tuple[str, str], separator: str, resolution: str,
) -> None:
    """
    年份后的分辨率、片源与编码标签在标题和路径入口均不产生季集。
    """
    left, right = brackets
    stem = f"某片名 2026{separator}{left}{resolution}{right}{left}BDRIP{right}{left}AV1.OPUS{right}"
    assert not is_anime(stem)

    for meta in (MetaInfo(stem + ".mkv"), MetaInfoPath(Path("/movies") / (stem + ".mkv"))):
        assert meta.name == "某片名"
        assert meta.year == "2026"
        assert meta.type == MediaType.UNKNOWN
        assert meta.begin_season is None
        assert meta.begin_episode is None
        assert meta.season_episode == ""
        assert meta.resource_pix == resolution.lower()
        assert meta.resource_type == "BDRIP"
        assert meta.video_encode == "AV1"
        assert meta.audio_encode == "OPUS"


@pytest.mark.usefixtures("recognition_backend")
@pytest.mark.parametrize(
    "stem",
    [
        "[字幕组] 某番剧 - 01 [1080p][BDRIP]",
        "[字幕组][某番剧][01][1080p][BDRIP]",
        "【字幕组】【某番剧】【01】【1080p】【BDRIP】",
        "某番剧 S01E01 [1080p][BDRIP]",
    ],
)
def test_episode_evidence_survives_resource_brackets(stem: str) -> None:
    """
    真正的数字方括号、横线集号和显式季集仍应识别为电视剧。
    """
    meta = MetaInfo(stem + ".mkv")
    assert meta.type == MediaType.TV
    assert meta.begin_episode == 1
    assert meta.episode == "E01"


@pytest.mark.parametrize("episode", ["01", "01-12", "01v2", "I", "IV", "1080"])
@pytest.mark.parametrize("brackets", [("[", "]"), ("【", "】")])
def test_numeric_and_roman_brackets_still_select_anime(
    episode: str, brackets: tuple[str, str],
) -> None:
    """
    只排除带扫描方式后缀的分辨率，保留数字、范围、修订版与罗马数字集号。
    """
    left, right = brackets
    assert is_anime(f"某番剧{left}{episode}{right}{left}1080p{right}")
