"""音乐识别统一入口路由测试。

覆盖 MediaChain 同步/异步 ``recognize_by_meta`` 与 ``recognize_by_path`` 按
``MetaMusic`` 路由到 ``MusicChain``，以及 ``MusicChain.recognize_by_meta`` 自身的
详情、搜索匹配与离线兜底分支。
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.chain.media import MediaChain
from app.chain.music import MusicChain
from app.core.context import MusicInfo
from app.core.meta import MetaMusic
from app.schemas.types import MediaType


def _music_info() -> MusicInfo:
    """构造带远端身份的标准音乐信息，用于断言路由返回值。"""
    return MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def test_media_chain_recognize_by_meta_routes_metamusic_to_musicchain():
    """MetaMusic 应绕过影视识别链，直接交给 MusicChain.recognize_by_meta。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = _music_info()
    music_chain = Mock()
    music_chain.recognize_by_meta = Mock(return_value=expected)

    with patch("app.chain.music.MusicChain", return_value=music_chain):
        result = MediaChain().recognize_by_meta(meta, source="musicbrainz")

    music_chain.recognize_by_meta.assert_called_once_with(meta, source="musicbrainz")
    assert result is expected


def test_media_chain_async_recognize_by_meta_routes_metamusic_to_musicchain():
    """异步识别同样应把 MetaMusic 路由到 MusicChain.async_recognize_by_meta。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = _music_info()
    music_chain = Mock()
    music_chain.async_recognize_by_meta = AsyncMock(return_value=expected)

    async def runner():
        with patch("app.chain.music.MusicChain", return_value=music_chain):
            return await MediaChain().async_recognize_by_meta(meta, source="musicbrainz")

    result = asyncio.run(runner())
    music_chain.async_recognize_by_meta.assert_awaited_once_with(meta, source="musicbrainz")
    assert result is expected


def test_media_chain_recognize_by_path_routes_audio_file_to_musicchain():
    """音频文件路径应经 MetaInfoPath 构造 MetaMusic 并路由到音乐识别链。"""
    expected = _music_info()
    music_chain = Mock()
    music_chain.recognize_by_meta = Mock(return_value=expected)

    with patch("app.chain.music.MusicChain", return_value=music_chain):
        context = MediaChain().recognize_by_path("/music/周杰伦 - 晴天.flac")

    routed_meta = music_chain.recognize_by_meta.call_args.args[0]
    assert isinstance(routed_meta, MetaMusic)
    assert context.media_info is expected
    assert isinstance(context.meta_info, MetaMusic)


def test_music_chain_recognize_by_meta_uses_detail_when_meta_has_identity(monkeypatch):
    """meta 携带 source+media_id 时应走详情分支，不再触发搜索。"""
    chain = MusicChain()
    meta = MetaMusic(title="晴天", media_source="musicbrainz", media_id="recording-1")
    expected = _music_info()
    monkeypatch.setattr(chain, "recognize", Mock(return_value=expected))
    search_mock = Mock(return_value=[])
    monkeypatch.setattr(chain, "run_module", search_mock)

    result = chain.recognize_by_meta(meta, source="musicbrainz")

    chain.recognize.assert_called_once_with("musicbrainz", "recording-1")
    search_mock.assert_not_called()
    assert result is expected


def test_music_chain_recognize_by_meta_matches_search_candidate(monkeypatch):
    """无身份时应按标题搜索并选择匹配候选。"""
    chain = MusicChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")
    candidate = _music_info()
    monkeypatch.setattr(chain, "run_module", Mock(return_value=[candidate]))

    result = chain.recognize_by_meta(meta)

    assert result is candidate


def test_music_chain_recognize_by_meta_falls_back_to_offline_when_no_match(monkeypatch):
    """搜索无候选时应返回离线兜底，且兜底结果不带远端 source。"""
    chain = MusicChain()
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    monkeypatch.setattr(chain, "run_module", Mock(return_value=[]))

    result = chain.recognize_by_meta(meta)

    assert result is not None
    assert result.title == "未知曲目"
    # 离线兜底不携带远端来源，订阅等场景据此判定未真实命中
    assert result.source is None
