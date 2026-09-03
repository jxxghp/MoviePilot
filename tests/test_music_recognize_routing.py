"""音乐识别统一入口路由测试。

覆盖 MediaChain 同步/异步 ``recognize_by_meta`` 与 ``recognize_by_path`` 按
``MetaMusic`` 路由到音乐模块，以及 MusicBrainz 模块 ``recognize_media`` /
``async_recognize_media`` 对音乐请求的详情、搜索匹配与兜底分支，MediaChain
对 MusicInfo 结果与影视统一走共享识别上报。
"""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.chain.acoustid import AcoustIdChain
from app.chain.media import MediaChain
from app.domain.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.modules.themoviedb import TheMovieDbModule
from app.runtime.config import ConfigModel
from app.schemas.types import MediaSource, MediaType


def _music_info() -> MusicInfo:
    """构造带远端身份的标准音乐信息，用于断言路由返回值。"""
    return MusicInfo(
        media_source="musicbrainz",
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

    result = chain.recognize_by_meta(
        meta, media_source="musicbrainz", mtype=MediaType.MUSIC
    )

    # 音乐不再旁路辅助识别选择流程，原生识别带共享元数据与剧集组参数
    chain.recognize_media.assert_called_once()
    call_kwargs = chain.recognize_media.call_args.kwargs
    assert call_kwargs["meta"] is meta
    assert call_kwargs["mtype"] == MediaType.MUSIC
    assert call_kwargs["media_source"] == "musicbrainz"
    assert result is expected


def test_media_chain_async_recognize_by_meta_routes_metamusic_to_module(monkeypatch):
    """异步识别同样应把 MetaMusic 路由到统一模块识别入口。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = _music_info()
    chain = MediaChain()
    monkeypatch.setattr(chain, "async_recognize_media", AsyncMock(return_value=expected))

    async def runner():
        return await chain.async_recognize_by_meta(
            meta, media_source="musicbrainz", mtype=MediaType.MUSIC
        )

    result = asyncio.run(runner())
    chain.async_recognize_media.assert_awaited_once()
    call_kwargs = chain.async_recognize_media.await_args.kwargs
    assert call_kwargs["meta"] is meta
    assert call_kwargs["mtype"] == MediaType.MUSIC
    assert call_kwargs["media_source"] == "musicbrainz"
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

    context = MediaChain().recognize_by_path("/downloads/晴天", media_source="musicbrainz")

    recognize_music.assert_called_once()
    assert recognize_music.call_args.kwargs["media_source"] == "musicbrainz"
    assert context.media_info is expected_info


def test_async_recognize_music_by_path_reads_local_audio_tags(tmp_path, monkeypatch):
    """本地音频识别应使用内嵌标签补全艺术家、专辑并保留音频质量参数。"""
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
    filename_meta = MetaMusic(title="02. 眼泪成诗")
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(meta, meta, filename_meta)),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "async_identify_music_by_fingerprint",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(chain, "async_recognize_media", recognize)

    recognized_meta, recognized_info = asyncio.run(
        chain.async_recognize_music_by_path(audio_path)
    )

    assert recognized_meta is meta
    assert recognized_info is info
    recognize.assert_awaited_once_with(
        meta=meta,
        media_source=None,
        music_type="recording",
    )


def test_recognize_music_by_path_fingerprint_mbid_skips_later_tiers(monkeypatch):
    """AcoustID 命中后应按 MBID 直查，且不再执行标签和文件名匹配。"""
    recording_id = "38035858-f990-4fbb-b3b2-f2f8b958eeba"
    merged = MetaMusic(title="Get Lucky", audio_format="FLAC")
    tag_meta = MetaMusic(title="Tagged Title")
    filename_meta = MetaMusic(title="Filename Title")
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id=recording_id,
        title="Get Lucky",
    )
    chain = MediaChain()
    direct = Mock(return_value=expected)
    later_tier = Mock()
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(merged, tag_meta, filename_meta)),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "identify_music_by_fingerprint",
        Mock(return_value=recording_id),
    )
    monkeypatch.setattr(chain, "_recognize_musicbrainz_recording", direct)
    monkeypatch.setattr(chain, "_recognize_music_meta_tier", later_tier)

    recognized_meta, recognized_info = chain.recognize_music_by_path("track.flac")

    assert recognized_meta is merged
    assert recognized_info is expected
    direct.assert_called_once_with(merged, recording_id)
    later_tier.assert_not_called()


def test_recognize_music_by_path_tag_mbid_skips_multi_source_matching(monkeypatch):
    """指纹未命中但标签含 MBID 时应直查详情，不进入多来源标题匹配。"""
    recording_id = "38035858-f990-4fbb-b3b2-f2f8b958eeba"
    tag_meta = MetaMusic(
        title="Tagged Title",
        media_source="musicbrainz",
        media_id=recording_id,
    )
    filename_meta = MetaMusic(title="Filename Title")
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id=recording_id,
        title="Tagged Title",
    )
    chain = MediaChain()
    direct = Mock(return_value=expected)
    generic = Mock()
    tier = Mock(wraps=chain._recognize_music_meta_tier)
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(tag_meta, tag_meta, filename_meta)),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "identify_music_by_fingerprint",
        Mock(return_value=None),
    )
    monkeypatch.setattr(chain, "_recognize_musicbrainz_recording", direct)
    monkeypatch.setattr(chain, "recognize_media", generic)
    monkeypatch.setattr(chain, "_recognize_music_meta_tier", tier)

    _, recognized_info = chain.recognize_music_by_path("track.flac")

    assert recognized_info is expected
    direct.assert_called_once_with(meta=tag_meta, recording_id=recording_id)
    generic.assert_not_called()
    assert tier.call_count == 1
    assert tier.call_args.kwargs["tier_name"] == "文件标签"


def test_recognize_music_by_path_falls_back_from_tags_to_filename(monkeypatch):
    """标签层未获得远端身份时应继续使用文件名层，且顺序不可反转。"""
    tag_meta = MetaMusic(title="Tagged Title")
    filename_meta = MetaMusic(title="Filename Title")
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-from-filename",
        title="Filename Title",
    )
    chain = MediaChain()
    recognize = Mock(side_effect=[MusicInfo(title="Offline Tag"), expected])
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(tag_meta, tag_meta, filename_meta)),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "identify_music_by_fingerprint",
        Mock(return_value=None),
    )
    monkeypatch.setattr(chain, "recognize_media", recognize)

    _, recognized_info = chain.recognize_music_by_path("track.flac")

    assert recognized_info is expected
    assert [call.kwargs["meta"] for call in recognize.call_args_list] == [
        tag_meta,
        filename_meta,
    ]
    assert all(
        call.kwargs["music_type"] == "recording"
        for call in recognize.call_args_list
    )


def test_async_recognize_music_by_path_fingerprint_mbid_skips_later_tiers(monkeypatch):
    """异步路径也应在 AcoustID 命中后直查 MBID 并停止后续层级。"""
    recording_id = "38035858-f990-4fbb-b3b2-f2f8b958eeba"
    merged = MetaMusic(title="Get Lucky")
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id=recording_id,
        title="Get Lucky",
    )
    chain = MediaChain()
    direct = AsyncMock(return_value=expected)
    later_tier = AsyncMock()
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(merged, MetaMusic(), MetaMusic())),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "async_identify_music_by_fingerprint",
        AsyncMock(return_value=recording_id),
    )
    monkeypatch.setattr(chain, "_async_recognize_musicbrainz_recording", direct)
    monkeypatch.setattr(chain, "_async_recognize_music_meta_tier", later_tier)

    recognized_meta, recognized_info = asyncio.run(
        chain.async_recognize_music_by_path("track.flac")
    )

    assert recognized_meta is merged
    assert recognized_info is expected
    direct.assert_awaited_once_with(merged, recording_id)
    later_tier.assert_not_awaited()


def test_musicbrainz_module_recognize_media_ignores_non_music():
    """非音乐请求应直接返回 None，不占用影视识别管线。"""
    result = MusicBrainzModule().recognize_media(
        meta=None,
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="123",
    )
    assert result is None


def test_chain_explicit_music_source_bypasses_generic_module_dispatch(monkeypatch):
    """显式音乐类型和来源应只调用对应音乐模块，不遍历通用影视模块。"""
    expected = _music_info()
    chain = MediaChain()
    recognize_source = Mock(return_value=expected)
    generic_dispatch = Mock()
    monkeypatch.setattr(chain, "recognize_music_from_source", recognize_source)
    monkeypatch.setattr(chain, "run_module", generic_dispatch)
    monkeypatch.setattr(chain.eventmanager, "check", Mock(return_value=False))

    with patch(
            "app.adapters.external.server.MoviePilotServerHelper.report_recognize_share"
    ):
        result = chain.recognize_media(
            mtype=MediaType.MUSIC,
            media_source="musicbrainz",
            media_id="recording-1",
        )

    assert result is expected
    recognize_source.assert_called_once_with(
        media_source=MediaSource.MusicBrainz,
        meta=None,
        media_id="recording-1",
        cache=True,
    )
    generic_dispatch.assert_not_called()


def test_media_chain_rejects_cross_entity_detail_result(monkeypatch):
    """指定专辑实体时，即使来源返回同 ID 的单曲也不得采信。"""
    chain = MediaChain()
    source_chain = Mock()
    source_chain.recognize_music.return_value = _music_info()
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))

    result = chain.recognize_music_from_source(
        media_source="musicbrainz",
        media_id="recording-1",
        music_type=MUSIC_ENTITY_ALBUM,
    )

    assert result is None
    assert source_chain.recognize_music.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM


def test_music_metadata_simplified_conversion_defaults_to_enabled():
    """音乐识别结果转简体开关应默认开启。"""
    assert ConfigModel.model_fields["MUSIC_METADATA_TO_SIMPLIFIED"].default is True


def test_media_chain_converts_recognized_music_metadata_without_mutating_source(monkeypatch):
    """开启开关时应转换标准音乐字段，并保留模块缓存对象和歌词原文。"""
    source_info = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="後來的我們",
        artists=["張學友"],
        album="歲月如歌",
        album_artist="張學友",
        category="華語流行",
        genres=["華語"],
        names=["後來的我們", "歲月如歌"],
        lyrics="後來的我們",
    )
    source_chain = Mock()
    source_chain.recognize_music.return_value = source_info
    chain = MediaChain()
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))
    monkeypatch.setattr(
        "app.runtime.config.settings.MUSIC_METADATA_TO_SIMPLIFIED", True
    )

    result = chain.recognize_music_from_source(
        media_source="musicbrainz",
        media_id="recording-1",
    )

    assert result is not source_info
    assert result.title == "后来的我们"
    assert result.artists == ["张学友"]
    assert result.album == "岁月如歌"
    assert result.album_artist == "张学友"
    assert result.metadata_category == "华语流行"
    assert result.category == ""
    assert result.genres == ["华语"]
    assert result.names == ["后来的我们", "岁月如歌"]
    assert result.lyrics == "後來的我們"
    assert source_info.title == "後來的我們"
    assert source_info.artists == ["張學友"]
    assert source_info.metadata_category == "華語流行"
    assert source_info.category == ""


def test_media_chain_preserves_original_music_metadata_when_conversion_disabled(monkeypatch):
    """关闭开关时识别结果应保持来源提供的原始繁简写法。"""
    source_info = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="後來的我們",
        artists=["張學友"],
    )
    source_chain = Mock()
    source_chain.recognize_music.return_value = source_info
    chain = MediaChain()
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))
    monkeypatch.setattr(
        "app.runtime.config.settings.MUSIC_METADATA_TO_SIMPLIFIED", False
    )

    result = chain.recognize_music_from_source(
        media_source="musicbrainz",
        media_id="recording-1",
    )

    assert result is source_info
    assert result.title == "後來的我們"
    assert result.artists == ["張學友"]


def test_music_path_fallback_converts_local_tag_metadata(monkeypatch):
    """远端未命中时，本地标签生成的音乐信息也应按开关转换为简体。"""
    meta = MetaMusic(
        title="後來的我們",
        artists=["張學友"],
        album="歲月如歌",
    )
    chain = MediaChain()
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(meta, meta, MetaMusic())),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "identify_music_by_fingerprint",
        Mock(return_value=None),
    )
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=None))
    monkeypatch.setattr(
        "app.runtime.config.settings.MUSIC_METADATA_TO_SIMPLIFIED", True
    )

    _, result = chain.recognize_music_by_path("track.flac")

    assert result.title == "后来的我们"
    assert result.artists == ["张学友"]
    assert result.album == "岁月如歌"
    assert meta.title == "後來的我們"


def test_media_chain_rejects_replaced_explicit_identity(monkeypatch):
    """显式 ID 识别不得用标题搜索得到的另一 ID 替换请求目标。"""
    chain = MediaChain()
    replaced = _music_info()
    replaced.media_id = "recording-other"
    source_chain = Mock()
    source_chain.recognize_music.return_value = replaced
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))

    result = chain.recognize_music_from_source(
        media_source="musicbrainz",
        media_id="recording-requested",
        music_type="recording",
    )

    assert result is None


def test_chain_music_type_rejects_video_source_before_module_dispatch(monkeypatch):
    """音乐状态即使携带错误影视来源，也不得调用 TMDB 等通用识别模块。"""
    chain = MediaChain()
    generic_dispatch = Mock()
    async_generic_dispatch = AsyncMock()
    monkeypatch.setattr(chain, "run_module", generic_dispatch)
    monkeypatch.setattr(chain, "async_run_module", async_generic_dispatch)
    monkeypatch.setattr(chain.eventmanager, "check", Mock(return_value=False))

    sync_result = chain.recognize_media(
        mtype=MediaType.MUSIC,
        media_source=MediaSource.TMDB,
        media_id="123",
    )
    async_result = asyncio.run(chain.async_recognize_media(
        mtype=MediaType.MUSIC,
        media_source=MediaSource.TMDB,
        media_id="123",
    ))

    assert sync_result is None
    assert async_result is None
    generic_dispatch.assert_not_called()
    async_generic_dispatch.assert_not_awaited()


def test_themoviedb_module_recognize_media_ignores_music(monkeypatch):
    """音乐模块未响应时 TMDB 的同步和异步入口均不得接管音乐请求。"""
    module = TheMovieDbModule()
    tmdb = Mock()
    monkeypatch.setattr(module, "tmdb", tmdb)

    by_meta = module.recognize_media(meta=MetaMusic(title="晴天"))
    by_type = module.recognize_media(mtype=MediaType.MUSIC, tmdbid=123)
    async_result = asyncio.run(
        module.async_recognize_media(meta=MetaMusic(title="晴天"))
    )

    assert by_meta is None
    assert by_type is None
    assert async_result is None
    assert tmdb.mock_calls == []


def test_bangumi_module_recognize_media_ignores_music(monkeypatch):
    """Bangumi 的同步和异步入口不得把音乐请求识别为动画影视。"""
    module = BangumiModule()
    api = Mock()
    monkeypatch.setattr(module, "bangumiapi", api)

    by_meta = module.recognize_media(
        meta=MetaMusic(title="晴天"), media_source=MediaSource.Bangumi
    )
    by_type = module.recognize_media(
        mtype=MediaType.MUSIC,
        media_source=MediaSource.Bangumi,
        media_id="123",
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=MetaMusic(title="晴天"), media_source=MediaSource.Bangumi
        )
    )

    assert by_meta is None
    assert by_type is None
    assert async_result is None
    assert api.mock_calls == []


def test_anilist_module_recognize_media_ignores_music(monkeypatch):
    """AniList 的同步和异步入口不得把音乐请求识别为动画影视。"""
    module = AniListModule()
    api = Mock()
    monkeypatch.setattr(module, "anilist_api", api)

    by_meta = module.recognize_media(
        meta=MetaMusic(title="晴天"), media_source=MediaSource.AniList
    )
    by_type = module.recognize_media(
        mtype=MediaType.MUSIC,
        media_source=MediaSource.AniList,
        media_id="123",
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=MetaMusic(title="晴天"), media_source=MediaSource.AniList
        )
    )

    assert by_meta is None
    assert by_type is None
    assert async_result is None
    assert api.mock_calls == []


def test_chain_obtain_images_skips_music_modules(monkeypatch):
    """音乐封面来自音乐元数据链，同步和异步补图入口均不得调用影视模块。"""
    chain = MediaChain()
    run_module = Mock()
    async_run_module = AsyncMock()
    monkeypatch.setattr(chain, "run_module", run_module)
    monkeypatch.setattr(chain, "async_run_module", async_run_module)
    music = _music_info()

    result = chain.obtain_images(music)
    async_result = asyncio.run(chain.async_obtain_images(music))

    assert result is music
    assert async_result is music
    run_module.assert_not_called()
    async_run_module.assert_not_awaited()


def test_musicbrainz_module_recognize_media_uses_detail_when_meta_has_identity(monkeypatch):
    """meta 携带 source+media_id 时应走详情分支，不再触发搜索。"""
    module = MusicBrainzModule()
    meta = MetaMusic(title="晴天", media_source="musicbrainz", media_id="recording-1")
    expected = _music_info()
    monkeypatch.setattr(module, "recognize_music", Mock(return_value=expected))
    recording_search = Mock(return_value=[])
    monkeypatch.setattr(module, "_search_recordings", recording_search)

    result = module.recognize_media(meta=meta, media_source="musicbrainz")

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
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
    )

    module.recognize_music.assert_called_once_with("musicbrainz", "recording-1")
    assert result is expected


def test_musicbrainz_module_async_recognize_media(monkeypatch):
    """异步 MusicBrainz 识别应直接调用异步检索而不进入同步入口。"""
    module = MusicBrainzModule()
    expected = _music_info()
    async_search = AsyncMock(return_value=[expected])
    sync_mock = Mock(side_effect=AssertionError("异步识别不应调用同步入口"))
    monkeypatch.setattr(module, "_async_search_recordings", async_search)
    monkeypatch.setattr(module, "recognize_media", sync_mock)

    result = asyncio.run(module.async_recognize_media(
        meta=MetaMusic(title="晴天"), mtype=MediaType.MUSIC
    ))

    async_search.assert_awaited_once()
    sync_mock.assert_not_called()
    assert result is expected


def test_theaudiodb_module_async_recognize_media(monkeypatch):
    """异步 TheAudioDB 识别应直接调用异步检索而不进入同步入口。"""
    module = TheAudioDbModule()
    expected = MusicInfo(
        media_source="theaudiodb",
        media_id="track-1",
        title="晴天",
    )
    async_search = AsyncMock(return_value=[expected])
    sync_mock = Mock(side_effect=AssertionError("异步识别不应调用同步入口"))
    monkeypatch.setattr(module, "_async_search_tracks", async_search)
    monkeypatch.setattr(module, "recognize_media", sync_mock)

    result = asyncio.run(module.async_recognize_media(
        meta=MetaMusic(title="晴天"),
        mtype=MediaType.MUSIC,
        media_source="theaudiodb",
    ))

    async_search.assert_awaited_once()
    sync_mock.assert_not_called()
    assert result is expected


def test_chain_recognize_media_returns_musicinfo_and_reports_share():
    """MusicBrainz 自动识别返回的 MusicInfo 应只上报一次共享识别。"""
    expected = _music_info()
    chain = MediaChain()
    with patch.object(
        chain, "recognize_music_from_source", return_value=expected
    ), patch(
            "app.adapters.external.server.MoviePilotServerHelper.report_recognize_share"
    ) as report_mock:
        result = chain.recognize_media(meta=MetaMusic(title="晴天"))

    report_mock.assert_called_once()
    assert report_mock.call_args.kwargs["mediainfo"] is expected
    assert result is expected


def test_chain_async_recognize_media_returns_musicinfo_and_reports_share():
    """异步 MusicBrainz 自动识别结果应只上报一次共享识别。"""
    expected = _music_info()
    chain = MediaChain()
    with patch.object(
            chain,
            "async_recognize_music_from_source",
            AsyncMock(return_value=expected),
    ), patch(
        "app.adapters.external.server.MoviePilotServerHelper.async_report_recognize_share",
        AsyncMock(),
    ) as report_mock:
        async def runner():
            return await chain.async_recognize_media(meta=MetaMusic(title="晴天"))

        result = asyncio.run(runner())

    report_mock.assert_awaited_once()
    assert report_mock.call_args.kwargs["mediainfo"] is expected
    assert result is expected
