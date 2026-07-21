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
