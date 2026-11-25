"""
验证搜索、路径解析与订阅匹配在两种后端下保持一致的全局识别词回退及 @global 展开语义。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system import rust as rust_accel
from app.chain.search.result import _torrent_meta
from app.chain.search.subtitle import SearchSubtitleOwner
from app.chain.subscribe.match import _should_reparse_torrent_meta
from app.domain import metainfo as metainfo_module
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.domain.meta import words as words_module
from app.domain.meta.metabase import MetaBase
from app.domain.meta.words import WordsMatcher, resolve_custom_words
from app.runtime.config import settings
from app.schemas.types import MediaType

TITLE = "LINK CLICK S03 2021 2160p WEB-DL H.265 AAC-HHWEB"
SUBTITLE = "时光代理人 / Link Click III 第三季 | 第06集"
GLOBAL_WORD = f"{TITLE} => {TITLE.replace('S03', 'S04')}"


@pytest.fixture
def recognition_words(monkeypatch):
    """
    隔离全局识别词及解析配置缓存，避免规则污染其它测试。
    """
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
        pytest.param(
            ["@global", "S04 => S05"],
            5,
            [GLOBAL_WORD, "S04 => S05"],
            id="global-first-then-subscribe",
        ),
        pytest.param(
            ["S03 => S02", "@global"],
            2,
            ["S03 => S02"],
            id="subscribe-first-then-global",
        ),
        pytest.param(
            ["  @GLOBAL\r ", "S04 => S06"],
            6,
            [GLOBAL_WORD, "S04 => S06"],
            id="global-token-case-and-whitespace",
        ),
    ],
)
def test_custom_words_fallback_matches_across_parsers(
    monkeypatch, recognition_words, backend, entry, custom_words,
    expected_season, applied_words,
):
    """
    空列表沿用全局规则，非空列表默认独立覆盖，@global 按所在位置展开全局规则；真实 Rust 不得靠 Python 兜底通过。
    """
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


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_global_custom_words_preserve_resolution_normalization_with_subscribe_rule(
    monkeypatch, recognition_words, backend,
):
    """
    订阅级季集修正通过 @global 继承全局 4k => 2160p 通用规则，确保两个后端解析结果一致。
    """
    recognition_words[:] = [r"(?i)\b4k\b => 2160p"]
    metainfo_module.clear_rust_parse_options_cache()
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

    meta = metainfo_module.MetaInfo(
        "My Show S03E06 4k WEB-DL",
        custom_words=["@global", "S03 => S05"],
    )

    assert meta.season_episode == "S05 E06"
    assert meta.resource_pix == "2160p"
    assert meta.apply_words == [r"(?i)\b4k\b => 2160p", "S03 => S05"]


def test_resolve_custom_words_deduplicates_global_expansion_and_ignores_recursive_token(
    recognition_words,
):
    """
    重复声明 @global 仅展开一次全局规则以避免重复偏移，且全局词表自身误含 @global 时不递归也不当作屏蔽词。
    """
    recognition_words[:] = ["@global", "第 <> 集 >> EP+1"]
    metainfo_module.clear_rust_parse_options_cache()

    assert resolve_custom_words(["@global", "旧名 => 新名", " @GLOBAL "]) == [
        "第 <> 集 >> EP+1",
        "旧名 => 新名",
    ]
    parsed_title, applied = WordsMatcher().prepare(
        "旧名 @global 第03集",
        custom_words=["@global", "旧名 => 新名", "@global"],
    )
    assert parsed_title == "新名 @global 第04集"
    assert applied == ["第 <> 集 >> EP+1", "旧名 => 新名"]


def test_global_parenthesized_tmdb_rule_routes_to_python_parser_when_included_via_global(
    monkeypatch, recognition_words,
):
    """
    通过 @global 引入含圆括号 TMDB 标签的全局规则时，_requires_python_metainfo 必须分流到 Python 解析器。
    """
    recognition_words[:] = ["Special Title => Special Title (tmdbid=98765)"]
    metainfo_module.clear_rust_parse_options_cache()
    fake_accel = SimpleNamespace(
        parse_metainfo=Mock(side_effect=AssertionError("含圆括号 TMDB 标签时不应调用 Rust")),
        supports_extended_media_ids=lambda: True,
    )
    monkeypatch.setattr(metainfo_module, "get_metainfo_accelerator", lambda: fake_accel)

    meta = metainfo_module.MetaInfo(
        "Special Title S01E02",
        custom_words=["@global", "S01 => S02"],
    )

    assert meta.media_id == "98765"
    assert meta.season_episode == "S02 E02"


@pytest.mark.parametrize("global_words", [None, [], ()])
@pytest.mark.parametrize("custom_words", [None, []])
def test_empty_custom_words_without_global_rules(
    monkeypatch, recognition_words, global_words, custom_words,
):
    """
    未配置全局规则或返回非列表对象时，缺省与空列表均生成空识别词列表。
    """
    recognition_words.clear()
    monkeypatch.setattr(words_module, "_custom_words_provider", lambda: global_words)
    metainfo_module.clear_rust_parse_options_cache()

    assert resolve_custom_words(custom_words) == []
    assert metainfo_module._rust_parse_options(custom_words)["custom_words"] == []


def test_should_reparse_torrent_meta_avoids_redundant_global_only_reparse(
    recognition_words,
):
    """
    订阅匹配中仅在专属规则生效或候选缓存滞后于全局规则时触发重算，跳过仅命中全局规则的无关候选。
    """
    recognition_words[:] = ["4k => 2160p"]
    matcher = WordsMatcher()

    unrelated_info = TorrentInfo(title="Other Movie 2026 4k", description="")
    unrelated_meta = MetaBase("")
    unrelated_meta.org_string = "Other Movie 2026 2160p"
    assert not _should_reparse_torrent_meta(
        matcher, unrelated_info, unrelated_meta, ["@global", "S03 => S05"]
    )
    assert not _should_reparse_torrent_meta(
        matcher, unrelated_info, unrelated_meta, ["S03 => S05"]
    )

    stale_cache_meta = MetaBase("")
    stale_cache_meta.org_string = "Other Movie 2026 4k"
    assert _should_reparse_torrent_meta(
        matcher, unrelated_info, stale_cache_meta, ["@global", "S03 => S05"]
    )

    target_info = TorrentInfo(title="Target Show S03E01 4k", description="第01集")
    target_meta = MetaBase("")
    target_meta.org_string = "Target Show S03E01 2160p"
    assert _should_reparse_torrent_meta(
        matcher, target_info, target_meta, ["@global", "S03 => S05"]
    )
    assert _should_reparse_torrent_meta(
        matcher, target_info, target_meta, ["Target Show <> 4k >> EP+12"]
    )

    legacy_stub = SimpleNamespace(
        prepare=lambda title, custom_words=None: (title, ["hit"] if "S03" in title else [])
    )
    assert _should_reparse_torrent_meta(
        legacy_stub, target_info, target_meta, ["S03 => S05"]
    )
    assert not _should_reparse_torrent_meta(
        legacy_stub, unrelated_info, unrelated_meta, ["S03 => S05"]
    )
