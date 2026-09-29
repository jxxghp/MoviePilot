"""验证搜索与路径解析在两种后端下保持相同的全局识别词回退语义。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.adapters.system import rust as rust_accel
from app.chain.search.result import _torrent_meta
from app.chain.search.subtitle import SearchSubtitleOwner
from app.domain import metainfo as metainfo_module
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.domain.meta import words as words_module
from app.runtime.config import settings
from app.schemas.types import MediaType

TITLE = "LINK CLICK S03 2021 2160p WEB-DL H.265 AAC-HHWEB"
SUBTITLE = "时光代理人 / Link Click III 第三季 | 第06集"
GLOBAL_WORD = f"{TITLE} => {TITLE.replace('S03', 'S04')}"


@pytest.fixture
def recognition_words(monkeypatch):
    """隔离全局识别词及解析配置缓存，避免规则污染其它测试。"""
    global_words = [GLOBAL_WORD]
    monkeypatch.setattr(words_module, "_custom_words_provider", lambda: global_words)
    metainfo_module.clear_rust_parse_options_cache()
    try:
        yield global_words
    finally:
        metainfo_module.clear_rust_parse_options_cache()


@pytest.mark.parametrize("backend", ["python", "rust"])
@pytest.mark.parametrize("entry", ["title", "torrent", "subtitle", "path"])
@pytest.mark.parametrize(
    ("custom_words", "expected_season", "applied_words"),
    [
        pytest.param(None, 4, [GLOBAL_WORD], id="default-global"),
        pytest.param([], 4, [GLOBAL_WORD], id="empty-global"),
        pytest.param(["S03 => S05"], 5, ["S03 => S05"], id="explicit-override"),
        pytest.param(["UNMATCHED => OTHER"], 3, [], id="explicit-no-match"),
    ],
)
def test_custom_words_fallback_matches_across_parsers(
    monkeypatch, recognition_words, backend, entry, custom_words,
    expected_season, applied_words,
):
    """空列表沿用全局规则，非空列表独立覆盖；真实 Rust 不得靠 Python 兜底通过。"""
    assert recognition_words == [GLOBAL_WORD]
    if backend == "rust":
        if not rust_accel.is_available():
            pytest.skip("moviepilot_rust 扩展未安装")
        monkeypatch.setattr(settings, "RUST_ACCEL", True)
        monkeypatch.setattr(metainfo_module, "get_metainfo_accelerator", lambda: rust_accel)
        monkeypatch.setattr(
            metainfo_module, "_build_python_meta_info",
            Mock(side_effect=AssertionError("Rust 解析不应回退到 Python")),
        )
    else:
        monkeypatch.setattr(metainfo_module, "get_metainfo_accelerator", lambda: None)

    if entry == "torrent":
        meta = _torrent_meta(
            TorrentInfo(title=TITLE, description=SUBTITLE),
            custom_words=custom_words,
            mediainfo=MediaInfo(type=MediaType.TV),
        )
    elif entry == "subtitle":
        meta = SearchSubtitleOwner._build_subtitle_meta(
            TITLE, SubtitleInfo(title=TITLE, description=SUBTITLE),
            custom_words=custom_words,
        )
    elif entry == "path":
        meta = metainfo_module.MetaInfoPath(
            Path(f"/tv/{TITLE} E06.mkv"), custom_words=custom_words,
        )
    else:
        meta = metainfo_module.MetaInfo(TITLE, SUBTITLE, custom_words=custom_words)

    assert meta.season_episode == f"S{expected_season:02d} E06"
    assert meta.apply_words == applied_words
    if entry != "path":
        assert meta.org_string == TITLE.replace("S03", f"S{expected_season:02d}")


@pytest.mark.parametrize("global_words", [None, []])
@pytest.mark.parametrize("custom_words", [None, []])
def test_empty_custom_words_without_global_rules(
    monkeypatch, recognition_words, global_words, custom_words,
):
    """未配置全局规则时，缺省与空列表均生成空识别词配置。"""
    recognition_words.clear()
    monkeypatch.setattr(words_module, "_custom_words_provider", lambda: global_words)

    assert metainfo_module._rust_parse_options(custom_words)["custom_words"] == []
