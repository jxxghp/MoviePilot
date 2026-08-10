import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints.media import recognize
from app.core.context import MediaInfo
from app.core.meta import MetaMusic
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    "title",
    [
        "武神主宰 (2020)/武神主宰.S01E680.mp4",
        r"D:\武神主宰 (2020)\武神主宰.S01E680.mp4",
    ],
)
def test_recognize_uses_parent_metadata_for_media_file_path(title: str) -> None:
    """标题参数为媒体文件路径时应合并父目录中的名称和年份。"""
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
    """普通片名和网络地址不应误走文件路径解析。"""
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
    """MusicBrainz 数据源下普通标题应按音乐元数据解析并拆分艺术家。"""
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(return_value=None)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        asyncio.run(recognize(title="周杰伦 - 晴天", source="musicbrainz", _=Mock()))

    metainfo = chain.async_recognize_by_meta.await_args.args[0]
    assert isinstance(metainfo, MetaMusic)
    assert metainfo.title == "晴天"
    assert metainfo.artists == ["周杰伦"]
    assert chain.async_recognize_by_meta.await_args.kwargs.get("source") == "musicbrainz"


def test_recognize_audio_file_title_uses_music_meta_without_source() -> None:
    """音频文件名无需指定数据源即应进入音乐识别分支。"""
    chain = Mock()
    chain.async_recognize_by_meta = AsyncMock(return_value=None)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        asyncio.run(recognize(title="周杰伦 - 晴天.flac", _=Mock()))

    metainfo = chain.async_recognize_by_meta.await_args.args[0]
    assert isinstance(metainfo, MetaMusic)
    assert metainfo.audio_format
