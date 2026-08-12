from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.metainfo import MetaInfo, MetaInfoPath, find_metainfo
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    ("title", "source", "media_id"),
    [
        ("葬送的芙莉莲 {[bangumiid=400602;type=tv;s=1]}", "bangumi", "400602"),
        ("Frieren {[anilistid=154587;type=tv;s=1]}", "anilist", "154587"),
        ("Frieren [anilist=154587] S01E01", "anilist", "154587"),
    ],
)
def test_find_metainfo_supports_anime_source_ids(
    title: str,
    source: str,
    media_id: str,
) -> None:
    """显式动画来源标签应提取统一来源ID并从标题中移除。"""
    parsed_title, metainfo = find_metainfo(title)

    assert metainfo["media_source"] == source
    assert metainfo["media_id"] == media_id
    assert f"{source}id=" not in parsed_title
    assert f"{source}=" not in parsed_title


def test_metainfo_custom_words_support_anilist_id() -> None:
    """自定义识别词替换结果中的AniList ID应进入统一元数据字段。"""
    meta = MetaInfo(
        "Sousou no Frieren 01",
        custom_words=[
            "Sousou no Frieren => Frieren {[anilistid=154587;type=tv;s=1]}"
        ],
    )

    assert meta.media_source == "anilist"
    assert meta.media_id == "154587"
    assert meta.type == MediaType.TV
    assert meta.begin_season == 1


def test_metainfo_path_inherits_bangumi_id_from_parent() -> None:
    """文件路径识别应从父目录继承Bangumi来源ID。"""
    meta = MetaInfoPath(
        Path("/anime/葬送的芙莉莲 [bangumi=400602]/Frieren.S01E01.mkv")
    )

    assert meta.media_source == "bangumi"
    assert meta.media_id == "400602"
    assert meta.begin_season == 1
    assert meta.begin_episode == 1


def test_extended_ids_fall_back_when_installed_rust_is_old() -> None:
    """当前Rust扩展缺少新字段时应直接使用Python解析器。"""
    with patch(
        "app.core.metainfo.rust_accel.supports_extended_media_ids",
        return_value=False,
    ), patch(
        "app.core.metainfo.rust_accel.find_metainfo",
        side_effect=AssertionError("旧Rust扩展不应处理扩展来源ID"),
    ):
        _, metainfo = find_metainfo("Frieren [anilist=154587]")

    assert metainfo["media_source"] == "anilist"
    assert metainfo["media_id"] == "154587"


def test_generic_identity_falls_back_when_installed_rust_is_old() -> None:
    """旧 Rust 扩展缺少通用字段时应直接使用 Python 解析器。"""
    with patch(
        "app.core.metainfo.rust_accel.supports_unified_media_identity",
        return_value=False,
    ), patch(
        "app.core.metainfo.rust_accel.find_metainfo",
        side_effect=AssertionError("旧 Rust 扩展不应处理通用媒体身份"),
    ):
        _, metainfo = find_metainfo(
            "Frieren {[media_source=anilist;media_id=154587]}"
        )

    assert metainfo["media_source"] == "anilist"
    assert metainfo["media_id"] == "154587"


@pytest.mark.parametrize(
    "title",
    [
        "Movie {[media_source=themoviedb;media_id=0;type=movies]}",
        "Movie {[tmdbid=0;type=movies]}",
        "Movie [tmdbid=0]",
        "Anime [anilist=0]",
    ],
)
def test_python_metainfo_rejects_zero_identity_and_removes_tag(title: str) -> None:
    """Python 标签解析器应移除零值标签，但不得生成媒体身份。"""
    with patch("app.core.metainfo.rust_accel.find_metainfo", return_value=None):
        parsed_title, metainfo = find_metainfo(title)

    assert metainfo["media_source"] is None
    assert metainfo["media_id"] is None
    assert "=0" not in parsed_title


def test_metainfo_normalizes_zero_identity_from_old_rust_extension() -> None:
    """旧 Rust 扩展返回零值身份时，主程序边界仍应将统一对清空。"""
    rust_result = {
        "title": "Movie",
        "metainfo": {
            "media_source": "themoviedb",
            "media_id": "0",
            "tmdbid": 0,
        },
    }
    with patch(
        "app.core.metainfo.rust_accel.find_metainfo",
        return_value=rust_result,
    ):
        parsed_title, metainfo = find_metainfo("Movie [tmdbid=0]")

    assert parsed_title == "Movie"
    assert metainfo["media_source"] is None
    assert metainfo["media_id"] is None
    assert "tmdbid" not in metainfo
