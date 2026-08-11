"""音乐识别统一入口路由测试。

覆盖 MediaChain 同步/异步 ``recognize_by_meta`` 与 ``recognize_by_path`` 按
``MetaMusic`` 路由到音乐模块，以及 MusicBrainz 模块 ``recognize_media`` /
``async_recognize_media`` 对音乐请求的详情、搜索匹配与兜底分支，ChainBase
对 MusicInfo 结果与影视统一走共享识别上报。
"""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.chain.media import MediaChain
from app.chain.music import MusicChain
from app.core.context import MusicInfo
from app.core.meta import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule
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


def test_media_chain_recognize_by_meta_routes_metamusic_to_module(monkeypatch):
    """MetaMusic 应与影视共用选择流程，由统一模块分发直接响应。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = _music_info()
    chain = MediaChain()
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=expected))

    result = chain.recognize_by_meta(meta, source="musicbrainz")

    # 音乐不再旁路辅助识别选择流程，原生识别带共享元数据与剧集组参数
    chain.recognize_media.assert_called_once()
    call_kwargs = chain.recognize_media.call_args.kwargs
    assert call_kwargs["meta"] is meta
    assert call_kwargs["source"] == "musicbrainz"
    assert result is expected


def test_media_chain_async_recognize_by_meta_routes_metamusic_to_module(monkeypatch):
    """异步识别同样应把 MetaMusic 路由到统一模块识别入口。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = _music_info()
    chain = MediaChain()
    monkeypatch.setattr(chain, "async_recognize_media", AsyncMock(return_value=expected))

    async def runner():
        return await chain.async_recognize_by_meta(meta, source="musicbrainz")

    result = asyncio.run(runner())
    chain.async_recognize_media.assert_awaited_once()
    call_kwargs = chain.async_recognize_media.await_args.kwargs
    assert call_kwargs["meta"] is meta
    assert call_kwargs["source"] == "musicbrainz"
    assert result is expected


def test_media_chain_recognize_by_path_routes_audio_file_to_music_chain(monkeypatch):
    """音频文件路径应经统一入口分发到 MediaChain 的音乐路径识别实现。"""
    expected_meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected_info = _music_info()
    recognize_music = Mock(return_value=(expected_meta, expected_info))
    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_music)

    context = MediaChain().recognize_by_path("/music/周杰伦 - 晴天.flac")

    recognize_music.assert_called_once()
    assert recognize_music.call_args.args[0] == "/music/周杰伦 - 晴天.flac"
    assert context.meta_info is expected_meta
    assert context.media_info is expected_info


def test_media_chain_recognize_by_path_routes_musicbrainz_source_to_music_chain(monkeypatch):
    """显式指定 MusicBrainz 数据源的路径识别也应分发到音乐实现。"""
    expected_meta = MetaMusic(title="晴天")
    expected_info = _music_info()
    recognize_music = Mock(return_value=(expected_meta, expected_info))
    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_music)

    context = MediaChain().recognize_by_path("/downloads/晴天", source="musicbrainz")

    recognize_music.assert_called_once()
    assert recognize_music.call_args.kwargs["source"] == "musicbrainz"
    assert context.media_info is expected_info


def test_async_recognize_music_by_path_reads_local_audio_tags(tmp_path, monkeypatch):
    """本地音频识别应使用内嵌标签补全艺术家、专辑并保留音频质量参数。"""
    from unittest.mock import AsyncMock

    from app.helper.audio import AudioMetadataHelper

    audio_path = tmp_path / "02. 眼泪成诗.m4a"
    audio_path.write_bytes(b"audio")
    meta = MetaMusic(
        title="眼泪成诗",
        artists=["孙燕姿"],
        album="完美的一天",
        track_number=2,
        duration=221,
    )
    info = _music_info()
    chain = MediaChain()
    recognize = AsyncMock(return_value=info)
    monkeypatch.setattr(AudioMetadataHelper, "read", lambda path: meta)
    monkeypatch.setattr(chain, "async_recognize_media", recognize)

    recognized_meta, recognized_info = asyncio.run(
        chain.async_recognize_music_by_path(audio_path)
    )

    assert recognized_meta is meta
    assert recognized_info is info
    recognize.assert_awaited_once_with(meta=meta, source="musicbrainz")


def test_musicbrainz_module_recognize_media_ignores_non_music():
    """非音乐请求应直接返回 None，不占用影视识别管线。"""
    result = MusicBrainzModule().recognize_media(
        meta=None, mtype=MediaType.MOVIE, source="themoviedb", mediaid="123"
    )
    assert result is None


def test_musicbrainz_module_recognize_media_uses_detail_when_meta_has_identity(monkeypatch):
    """meta 携带 source+media_id 时应走详情分支，不再触发搜索。"""
    module = MusicBrainzModule()
    meta = MetaMusic(title="晴天", media_source="musicbrainz", media_id="recording-1")
    expected = _music_info()
    monkeypatch.setattr(module, "recognize_music", Mock(return_value=expected))
    recording_search = Mock(return_value=[])
    monkeypatch.setattr(module, "_search_recordings", recording_search)

    result = module.recognize_media(meta=meta, source="musicbrainz")

    module.recognize_music.assert_called_once_with("musicbrainz", "recording-1")
    recording_search.assert_not_called()
    assert result is expected


def test_musicbrainz_module_recognize_media_matches_search_candidate(monkeypatch):
    """无身份时应从 Recording 搜索中选择匹配候选。"""
    module = MusicBrainzModule()
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")
    candidate = _music_info()
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[candidate]))

    result = module.recognize_media(meta=meta)

    assert result is candidate


def test_musicbrainz_module_recognize_media_falls_back_to_offline_when_no_match(monkeypatch):
    """搜索无候选时应返回元数据兜底，且兜底结果不带远端身份。"""
    module = MusicBrainzModule()
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[]))
    monkeypatch.setattr(module, "_search_albums", Mock(return_value=[]))

    result = module.recognize_media(meta=meta)

    assert result is not None
    assert result.title == "未知曲目"
    assert isinstance(result, MusicInfo)


def test_musicbrainz_module_recognize_media_by_music_type_and_media_id(monkeypatch):
    """mtype 为音乐且指定数据源原生 ID 时应直接按音乐详情识别。"""
    module = MusicBrainzModule()
    expected = _music_info()
    monkeypatch.setattr(module, "recognize_music", Mock(return_value=expected))

    result = module.recognize_media(
        mtype=MediaType.MUSIC, source="musicbrainz", mediaid="recording-1"
    )

    module.recognize_music.assert_called_once_with("musicbrainz", "recording-1")
    assert result is expected


def test_musicbrainz_module_async_recognize_media(monkeypatch):
    """异步模块入口应在线程池中调用同步实现。"""
    module = MusicBrainzModule()
    expected = _music_info()
    sync_mock = Mock(return_value=expected)
    monkeypatch.setattr(module, "recognize_media", sync_mock)

    result = asyncio.run(module.async_recognize_media(
        meta=MetaMusic(title="晴天"), mtype=MediaType.MUSIC
    ))

    sync_mock.assert_called_once()
    assert result is expected


def test_chain_recognize_media_returns_musicinfo_and_reports_share():
    """ChainBase.recognize_media 收到 MusicInfo 结果应与影视统一上报共享识别。"""
    expected = _music_info()
    chain = MediaChain()
    chain.run_module = Mock(return_value=expected)
    with patch(
        "app.helper.server.MoviePilotServerHelper.report_recognize_share"
    ) as report_mock:
        result = chain.recognize_media(meta=MetaMusic(title="晴天"))

    report_mock.assert_called_once()
    assert report_mock.call_args.kwargs["mediainfo"] is expected
    assert result is expected


def test_chain_async_recognize_media_returns_musicinfo_and_reports_share():
    """异步 ChainBase 收到 MusicInfo 结果应与影视统一上报共享识别。"""
    expected = _music_info()
    chain = MediaChain()
    chain.async_run_module = AsyncMock(return_value=expected)
    with patch(
        "app.helper.server.MoviePilotServerHelper.async_report_recognize_share",
        AsyncMock(),
    ) as report_mock:
        async def runner():
            return await chain.async_recognize_media(meta=MetaMusic(title="晴天"))

        result = asyncio.run(runner())

    report_mock.assert_awaited_once()
    assert report_mock.call_args.kwargs["mediainfo"] is expected
    assert result is expected
