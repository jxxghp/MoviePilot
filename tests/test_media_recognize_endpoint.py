"""
验证媒体识别接口的路径解析、来源选择和元数据传递。
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints.media import recognize
from app.domain.context import MediaInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    "title",
    [
        "武神主宰 (2020)/武神主宰.S01E680.mp4",
        r"D:\武神主宰 (2020)\武神主宰.S01E680.mp4",
    ],
)
def test_recognize_uses_parent_metadata_for_media_file_path(title: str) -> None:
    """
    标题参数为媒体文件路径时应合并父目录中的名称和年份。
    """
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(
        return_value=MediaInfo(
            title="武神主宰",
            year="2020",
            type=MediaType.TV,
        )
    )

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        asyncio.run(recognize(title=title, _=Mock()))

    metainfo = chain.async_recognize_by_meta.await_args.args[0]
    assert metainfo.name == "武神主宰"
    assert metainfo.year == "2020"
    assert metainfo.begin_season == 1
    assert metainfo.begin_episode == 680
    assert metainfo.title == title


@pytest.mark.parametrize(
    "title",
    [
        "Fate/stay night",
        "https://example.com/武神主宰.S01E680.mp4",
    ],
)
def test_recognize_does_not_treat_non_path_title_as_file_path(title: str) -> None:
    """
    普通片名和网络地址不应误走文件路径解析。
    """
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(
        return_value=MediaInfo(title="Fate/stay night", type=MediaType.TV)
    )

    with (
        patch("app.api.endpoints.media.MediaChain", return_value=chain),
        patch("app.api.endpoints.media.MetaInfoPath") as meta_info_path,
    ):
        asyncio.run(recognize(title=title, _=Mock()))

    meta_info_path.assert_not_called()


def test_recognize_musicbrainz_source_parses_music_meta() -> None:
    """
    MusicBrainz 数据源下普通标题应按音乐元数据解析并拆分艺术家。
    """
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(return_value=None)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        asyncio.run(recognize(title="周杰伦 - 晴天", media_source="musicbrainz", _=Mock()))

    metainfo = chain.async_recognize_by_meta.await_args.args[0]
    assert isinstance(metainfo, MetaMusic)
    assert metainfo.title == "晴天"
    assert metainfo.artists == ["周杰伦"]
    assert chain.async_recognize_by_meta.await_args.kwargs.get("media_source") == "musicbrainz"


def test_recognize_audio_file_title_uses_music_meta_without_source() -> None:
    """
    音频文件名无需指定数据源即应进入音乐识别分支。
    """
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(return_value=None)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        asyncio.run(recognize(title="周杰伦 - 晴天.flac", _=Mock()))

    metainfo = chain.async_recognize_by_meta.await_args.args[0]
    assert isinstance(metainfo, MetaMusic)
    assert metainfo.audio_format


def test_recognize_returns_meta_info_when_media_lookup_fails() -> None:
    """
    未匹配到媒体时也应返回解析元数据，供识别测试展示识别词应用情况和解析结果。
    """
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(return_value=None)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = asyncio.run(
            recognize(
                title="Some.Release.2024",
                custom_words="Some => Other",
                _=Mock(),
            )
        )

    assert result["media_info"] is None
    assert result["meta_info"]["org_string"] == "Other.Release.2024"
    assert result["meta_info"]["apply_words"] == ["Some => Other"]
