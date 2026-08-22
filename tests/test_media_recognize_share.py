"""共享媒体识别链路测试。

覆盖本地识别成功后上报共享识别、本地识别失败后回查共享识别并二次识别、
共享识别成功后回填本地缓存、音乐识别上报/查询载荷，以及命中缓存不重复上报等场景。
"""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from app.application.orchestration import ChainBase
from app.application.orchestration.media import MediaChain
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.adapters.external.server import MoviePilotServerHelper
from app.schemas.types import MediaSource, MediaType


def _build_meta(name: str, media_type: MediaType = MediaType.UNKNOWN) -> MetaBase:
    """构造测试用元数据。"""
    meta = MetaBase(name)
    meta.name = name
    meta.type = media_type
    return meta


def _tmdb_media(
        title: str,
        media_id: int,
        media_type: MediaType,
        **kwargs,
) -> MediaInfo:
    """构造同时带规范主身份和 TMDB 辅助元数据的测试媒体。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id=str(media_id),
        tmdb_id=media_id,
        title=title,
        type=media_type,
        **kwargs,
    )


def _enable_media_recognize_share(chain: ChainBase) -> None:
    """为单个链实例启用共享识别配置快照。"""
    chain.runtime_config = replace(
        chain.runtime_config,
        media_recognize_share=True,
    )


def test_report_shared_result_after_local_recognize_success():
    """本地识别成功后应上报共享识别结果。"""
    chain = ChainBase()
    meta = _build_meta("测试电影", MediaType.MOVIE)
    mediainfo = _tmdb_media("测试电影", 100, MediaType.MOVIE, year="2024")

    with patch.object(chain, "unicast", return_value=mediainfo) as unicast, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is mediainfo
    unicast.assert_called_once()
    report_mock.assert_called_once_with(meta=meta, mediainfo=mediainfo, keyword_meta=meta)
    query_mock.assert_not_called()


def test_query_shared_result_when_local_recognize_failed():
    """本地识别失败后应回查共享识别结果，并按共享ID再次识别。"""
    chain = ChainBase()
    _enable_media_recognize_share(chain)
    meta = _build_meta("测试剧集")
    shared_media = _tmdb_media("测试剧集", 200, MediaType.TV, year="2024")

    with patch.object(
        chain,
        "unicast",
        side_effect=[None, shared_media],
    ) as unicast, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "tv",
            "media_source": "themoviedb",
            "media_id": "200",
            "season": 1,
        },
    ) as query_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.TV,
            "media_source": MediaSource.TMDB,
            "media_id": "200",
            "season": 1,
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is shared_media
    assert unicast.call_count == 2
    query_mock.assert_called_once_with(meta=meta, mtype=None, keyword_meta=meta)
    second_call = unicast.call_args_list[1]
    assert second_call.kwargs["media_source"] == MediaSource.TMDB
    assert second_call.kwargs["media_id"] == "200"
    assert second_call.kwargs["mtype"] == MediaType.TV
    assert meta.begin_season is None


def test_async_query_shared_result_when_local_recognize_failed():
    """异步识别失败后也应回查共享识别结果。"""
    chain = ChainBase()
    _enable_media_recognize_share(chain)
    meta = _build_meta("测试异步剧集")
    shared_media = _tmdb_media("测试异步剧集", 300, MediaType.TV, year="2025")
    async_unicast = AsyncMock(side_effect=[None, shared_media])

    async def runner():
        with patch.object(
            chain,
            "async_unicast",
            async_unicast,
        ), patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.async_query_recognize_share",
            AsyncMock(return_value={
                "type": "tv",
                "media_source": "themoviedb",
                "media_id": "300",
                "season": 2,
            }),
        ) as query_mock, patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
            return_value={
                "mtype": MediaType.TV,
                "media_source": MediaSource.TMDB,
                "media_id": "300",
                "season": 2,
            },
        ), patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.async_report_recognize_share",
            AsyncMock(return_value=False),
        ), patch.object(
            chain,
            "_async_update_local_recognize_cache",
            AsyncMock(),
        ) as backfill_mock:
            result = await chain.async_recognize_media(meta=meta, cache=False)
        return result, query_mock, backfill_mock

    result, query_mock, backfill_mock = asyncio.run(runner())

    assert result is shared_media
    assert async_unicast.await_count == 2
    query_mock.assert_awaited_once_with(meta=meta, mtype=None, keyword_meta=meta)
    backfill_mock.assert_awaited_once()
    assert meta.begin_season is None


def test_backfill_local_cache_after_shared_recognize_success():
    """共享识别后二次本地识别成功时，应回填原始名称对应的本地识别缓存。"""
    chain = ChainBase()
    _enable_media_recognize_share(chain)
    meta = _build_meta("测试缓存回填", MediaType.MOVIE)
    shared_media = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="700",
        title="测试缓存回填",
        year="2024",
        tmdb_id=700,
        type=MediaType.MOVIE,
        tmdb_info={"id": 700, "media_type": MediaType.MOVIE, "title": "测试缓存回填"},
    )

    with patch.object(
        chain,
        "unicast",
        side_effect=[None, shared_media],
    ) as unicast_mock, patch.object(
        chain,
        "broadcast",
    ) as broadcast_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "movie",
            "media_source": "themoviedb",
            "media_id": "700",
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MOVIE,
            "media_source": MediaSource.TMDB,
            "media_id": "700",
            "season": None,
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is shared_media
    assert unicast_mock.call_count == 2
    broadcast_mock.assert_called_once()
    update_call = broadcast_mock.call_args
    assert update_call.args[0] == "update_recognize_cache"
    assert update_call.kwargs["meta"] is not meta
    assert update_call.kwargs["meta"].name == meta.name
    assert update_call.kwargs["meta"].type == meta.type
    assert update_call.kwargs["mediainfo"] is shared_media


def test_query_and_report_prefer_original_name_keyword():
    """查询和上报共享识别时应优先使用未应用识别词的识别名称。"""
    meta = _build_meta("应用识别词后的名称", MediaType.TV)
    meta.original_name = "未应用识别词的名称"
    meta.year = "2024"
    meta.begin_season = 1
    mediainfo = _tmdb_media(
        "测试剧集", 400, MediaType.TV, year="2024", season=1
    )

    query_params = MoviePilotServerHelper._build_recognize_query_params(meta=meta)
    report_payload = MoviePilotServerHelper._build_recognize_report_payload(meta=meta, mediainfo=mediainfo)

    assert query_params["keyword"] == "未应用识别词的名称"
    assert report_payload["keyword"] == "未应用识别词的名称"


def test_query_and_report_can_use_distinct_keyword_meta():
    """共享识别应允许用原始关键字上报，同时保留辅助识别后的年份/季信息。"""
    meta = _build_meta("辅助识别后的名称", MediaType.TV)
    meta.year = "2024"
    meta.begin_season = 2

    keyword_meta = _build_meta("辅助识别前的名称", MediaType.UNKNOWN)
    keyword_meta.original_name = "辅助识别前的名称"

    mediainfo = _tmdb_media(
        "测试剧集", 401, MediaType.TV, year="2024", season=2
    )

    query_params = MoviePilotServerHelper._build_recognize_query_params(
        meta=meta,
        mtype=None,
        keyword_meta=keyword_meta,
    )
    report_payload = MoviePilotServerHelper._build_recognize_report_payload(
        meta=meta,
        mediainfo=mediainfo,
        keyword_meta=keyword_meta,
    )

    assert query_params["keyword"] == "辅助识别前的名称"
    assert query_params["year"] == "2024"
    assert query_params["season"] == 2
    assert report_payload["keyword"] == "辅助识别前的名称"
    assert report_payload["year"] == "2024"
    assert report_payload["season"] == 2


def test_query_and_report_preserve_special_season_zero():
    """共享识别查询和上报都必须保留显式特别季。"""
    meta = _build_meta("测试剧特别篇", MediaType.TV)
    meta.begin_season = 0
    mediainfo = _tmdb_media("测试剧", 402, MediaType.TV, season=0)

    query_params = MoviePilotServerHelper._build_recognize_query_params(meta=meta)
    report_payload = MoviePilotServerHelper._build_recognize_report_payload(
        meta=meta,
        mediainfo=mediainfo,
    )

    assert query_params["season"] == 0
    assert report_payload["season"] == 0


def test_plugin_recognize_number_parser_preserves_zero():
    """插件辅助识别应同时接受整数和字符串形式的季 0。"""
    media_chain = MediaChain()
    assert media_chain._parse_recognize_event_number(0) == 0
    assert media_chain._parse_recognize_event_number("0") == 0
    assert media_chain._parse_recognize_event_number(None) is None
    assert media_chain._parse_recognize_event_number("invalid") is None


def test_report_shared_result_with_distinct_keyword_meta():
    """辅助识别成功后应按辅助前名称上报共享结果。"""
    chain = ChainBase()
    meta = _build_meta("辅助识别后的名称", MediaType.TV)
    meta.year = "2024"
    meta.begin_season = 1
    share_meta = _build_meta("辅助识别前的名称", MediaType.UNKNOWN)
    share_meta.original_name = "辅助识别前的名称"
    mediainfo = _tmdb_media("测试剧集", 402, MediaType.TV, year="2024")

    with patch.object(chain, "unicast", return_value=mediainfo), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock:
        result = chain.recognize_media(meta=meta, share_meta=share_meta, cache=False)

    assert result is mediainfo
    report_mock.assert_called_once_with(
        meta=meta,
        mediainfo=mediainfo,
        keyword_meta=share_meta,
    )


def test_query_shared_result_with_distinct_keyword_meta():
    """本地识别失败后应按辅助前名称回查共享结果。"""
    chain = ChainBase()
    _enable_media_recognize_share(chain)
    meta = _build_meta("辅助识别后的名称", MediaType.TV)
    meta.year = "2024"
    share_meta = _build_meta("辅助识别前的名称", MediaType.UNKNOWN)
    share_meta.original_name = "辅助识别前的名称"
    shared_media = _tmdb_media("测试剧集", 403, MediaType.TV, year="2024")

    with patch.object(
        chain,
        "unicast",
        side_effect=[None, shared_media],
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "tv",
            "media_source": "themoviedb",
            "media_id": "403",
            "season": 1,
        },
    ) as query_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.TV,
            "media_source": MediaSource.TMDB,
            "media_id": "403",
            "season": 1,
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(
            meta=meta,
            share_meta=share_meta,
            cache=False,
        )

    assert result is shared_media
    query_mock.assert_called_once_with(
        meta=meta,
        mtype=MediaType.TV,
        keyword_meta=share_meta,
    )


def test_skip_report_when_local_recognize_hits_cache():
    """本地识别命中缓存时不应上报共享识别。"""
    chain = ChainBase()
    meta = _build_meta("缓存电影", MediaType.MOVIE)
    mediainfo = _tmdb_media("缓存电影", 500, MediaType.MOVIE, year="2024")
    mediainfo.recognize_cache_hit = True

    with patch.object(chain, "unicast", return_value=mediainfo) as unicast, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta)

    assert result is mediainfo
    unicast.assert_called_once()
    report_mock.assert_not_called()
    query_mock.assert_not_called()


def test_async_skip_report_when_local_recognize_hits_cache():
    """异步本地识别命中缓存时不应上报共享识别。"""
    chain = ChainBase()
    meta = _build_meta("缓存剧集", MediaType.TV)
    mediainfo = _tmdb_media("缓存剧集", 600, MediaType.TV, year="2025")
    mediainfo.recognize_cache_hit = True

    async def runner():
        with patch.object(
            chain,
            "async_unicast",
            AsyncMock(return_value=mediainfo),
        ) as async_unicast, patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.async_report_recognize_share",
            AsyncMock(return_value=True),
        ) as report_mock, patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.async_query_recognize_share",
            AsyncMock(),
        ) as query_mock:
            result = await chain.async_recognize_media(meta=meta)
        return result, async_unicast, report_mock, query_mock

    result, async_unicast, report_mock, query_mock = asyncio.run(runner())

    assert result is mediainfo
    async_unicast.assert_awaited_once()
    report_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


def test_recognize_by_meta_can_skip_obtain_images():
    """标题识别可显式关闭图片拉取。"""
    media_chain = MediaChain()
    meta = MetaInfo("测试电影")
    mediainfo = _tmdb_media("测试电影", 404, MediaType.MOVIE, year="2024")

    with patch.object(
        media_chain,
        "recognize_media",
        return_value=mediainfo,
    ) as recognize_mock, patch.object(
        media_chain,
        "obtain_images",
    ) as obtain_images_mock:
        result = media_chain.recognize_by_meta(meta, obtain_images=False)

    assert result is mediainfo
    recognize_mock.assert_called_once()
    obtain_images_mock.assert_not_called()


def test_recognize_by_meta_reports_with_original_keyword_after_plugin_help():
    """辅助识别后应继续使用辅助前关键字进行共享上报。"""
    media_chain = MediaChain()
    meta = MetaInfo("辅助前名称")
    plugin_media = _tmdb_media("辅助后名称", 405, MediaType.TV, year="2024")

    with patch.object(
        media_chain,
        "select_recognize_source",
        side_effect=lambda **kwargs: kwargs["plugin_fn"](),
    ), patch.object(
        media_chain,
        "recognize_help",
        return_value=plugin_media,
    ) as recognize_help_mock, patch.object(
        media_chain,
        "obtain_images",
    ):
        result = media_chain.recognize_by_meta(meta, obtain_images=False)

    assert result is plugin_media
    assert recognize_help_mock.call_args.kwargs["share_meta"].name == "辅助前名称"


def test_async_recognize_by_meta_can_skip_obtain_images():
    """异步标题识别可显式关闭图片拉取。"""
    media_chain = MediaChain()
    meta = MetaInfo("测试异步电影")
    mediainfo = _tmdb_media("测试异步电影", 406, MediaType.MOVIE, year="2025")

    async def runner():
        with patch.object(
            media_chain,
            "async_recognize_media",
            AsyncMock(return_value=mediainfo),
        ) as recognize_mock, patch.object(
            media_chain,
            "async_obtain_images",
            AsyncMock(),
        ) as obtain_images_mock:
            result = await media_chain.async_recognize_by_meta(
                meta,
                obtain_images=False,
            )
        return result, recognize_mock, obtain_images_mock

    result, recognize_mock, obtain_images_mock = asyncio.run(runner())

    assert result is mediainfo
    recognize_mock.assert_awaited_once()
    obtain_images_mock.assert_not_called()


def _music_info() -> MusicInfo:
    """构造带远端身份的标准音乐信息。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def test_music_report_payload_includes_source_and_media_id():
    """音乐上报载荷应只携带统一来源、原生身份和音乐实体类型。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美", year=2003)
    payload = MoviePilotServerHelper._build_recognize_report_payload(
        meta=meta,
        mediainfo=_music_info(),
    )

    assert payload["keyword"] == "晴天"
    assert payload["type"] == "music"
    assert payload["title"] == "晴天"
    assert payload["year"] == "2003"
    assert payload["season"] is None
    assert "tmdbid" not in payload
    assert "doubanid" not in payload
    assert payload["media_source"] == "musicbrainz"
    assert payload["media_id"] == "recording-1"
    assert payload["music_type"] == "recording"


def test_music_report_payload_skips_fallback_without_remote_identity():
    """无远端身份的兜底音乐结果不应上报。"""
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    fallback = MusicInfo(title="未知曲目", artists=["未知艺术家"])
    payload = MoviePilotServerHelper._build_recognize_report_payload(
        meta=meta,
        mediainfo=fallback,
    )

    assert payload is None


def test_music_query_params_build_for_music_type():
    """音乐查询参数应携带 music 类型、年份且不包含季。"""
    meta = MetaMusic(title="晴天", year=2003)
    keyword_meta = MetaMusic(title="晴天")

    params = MoviePilotServerHelper._build_recognize_query_params(
        meta=meta,
        mtype=MediaType.MUSIC,
        keyword_meta=keyword_meta,
    )

    assert params["keyword"] == "晴天"
    assert params["type"] == "music"
    assert params["year"] == "2003"
    assert params["music_type"] == "recording"
    assert "season" not in params

    album_params = MoviePilotServerHelper._build_recognize_query_params(
        meta=meta,
        mtype=MediaType.MUSIC,
        keyword_meta=keyword_meta,
        music_type="album",
    )
    assert album_params["music_type"] == "album"


def test_music_shared_identity_keeps_entity_type():
    """共享专辑身份转回本地识别参数时必须保留专辑命名空间。"""
    params = MoviePilotServerHelper.to_recognize_params({
        "type": "music",
        "media_source": "musicbrainz",
        "media_id": "release-group-1",
        "music_type": "album",
    })

    assert params["mtype"] == MediaType.MUSIC
    assert params["media_source"] == MediaSource.MusicBrainz
    assert params["media_id"] == "release-group-1"
    assert params["music_type"] == "album"


def test_legacy_music_shared_identity_defaults_to_recording():
    """旧共享记录缺少实体字段时只按自动识别的 Recording 语义兼容。"""
    params = MoviePilotServerHelper.to_recognize_params({
        "type": "music",
        "media_source": "musicbrainz",
        "media_id": "recording-1",
    })

    assert params["music_type"] == "recording"


def test_chain_recognize_media_reports_music_share_result():
    """音乐识别成功且有远端身份时，应与影视一样走统一共享上报。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"], year=2003)
    music = _music_info()

    with patch.object(chain, "recognize_music_from_source", return_value=music), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    report_mock.assert_called_once_with(meta=meta, mediainfo=music, keyword_meta=meta)
    query_mock.assert_not_called()


def test_chain_recognize_media_queries_music_share_when_local_failed():
    """音乐本地识别失败后应回查共享识别并按数据源原生 ID 二次识别。"""
    chain = MediaChain()
    _enable_media_recognize_share(chain)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    music = _music_info()

    with patch.object(
        chain,
        "recognize_music_from_source",
        side_effect=[None, music],
    ) as recognize_source, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "music",
            "media_source": "musicbrainz",
            "media_id": "recording-1",
            "music_type": "recording",
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MUSIC,
            "media_source": MediaSource.MusicBrainz,
            "media_id": "recording-1",
            "music_type": "recording",
            "season": None,
        },
    ), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    assert recognize_source.call_count == 2
    recognize_source.assert_called_with(
        media_source="musicbrainz",
        meta=meta,
        media_id="recording-1",
        cache=False,
        music_type="recording",
    )


def test_chain_recognize_media_queries_music_share_after_local_fallback():
    """本地标签兜底没有远端身份时，仍应通过共享结果补成标准音乐身份。"""
    chain = MediaChain()
    _enable_media_recognize_share(chain)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = MusicInfo(title="晴天", artists=["周杰伦"])
    music = _music_info()

    with patch.object(
        chain,
        "recognize_music_from_source",
        side_effect=[fallback, music],
    ) as recognize_source, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "music",
            "media_source": "musicbrainz",
            "media_id": "recording-1",
            "music_type": "recording",
        },
    ) as query_share, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MUSIC,
            "media_source": MediaSource.MusicBrainz,
            "media_id": "recording-1",
            "music_type": "recording",
            "season": None,
        },
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    query_share.assert_called_once_with(
        meta=meta,
        mtype=MediaType.MUSIC,
        keyword_meta=meta,
    )
    assert recognize_source.call_count == 2


def test_chain_async_recognize_media_queries_music_share_after_local_fallback():
    """异步音乐识别也必须在返回本地兜底前尝试共享身份补全。"""
    chain = MediaChain()
    _enable_media_recognize_share(chain)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = MusicInfo(title="晴天", artists=["周杰伦"])
    music = _music_info()

    async def runner():
        with patch.object(
            chain,
            "async_recognize_music_from_source",
            new=AsyncMock(side_effect=[fallback, music]),
        ) as recognize_source, patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.async_query_recognize_share",
            new=AsyncMock(return_value={
                "type": "music",
                "media_source": "musicbrainz",
                "media_id": "recording-1",
                "music_type": "recording",
            }),
        ) as query_share, patch(
            "app.application.orchestration._recognition.MoviePilotServerHelper.to_recognize_params",
            return_value={
                "mtype": MediaType.MUSIC,
                "media_source": MediaSource.MusicBrainz,
                "media_id": "recording-1",
                "music_type": "recording",
                "season": None,
            },
        ), patch.object(
            chain,
            "_async_update_local_recognize_cache",
            new=AsyncMock(),
        ):
            result = await chain.async_recognize_media(meta=meta, cache=False)
        return result, query_share, recognize_source

    result, query_share, recognize_source = asyncio.run(runner())

    assert result is music
    query_share.assert_awaited_once_with(
        meta=meta,
        mtype=MediaType.MUSIC,
        keyword_meta=meta,
    )
    assert recognize_source.await_count == 2
    recognize_source.assert_awaited_with(
        media_source="musicbrainz",
        meta=meta,
        media_id="recording-1",
        cache=False,
        music_type="recording",
    )


def test_chain_recognize_media_skips_music_report_for_fallback_result():
    """共享也未命中时保留音乐标签兜底，且不把无身份结果上报。"""
    chain = MediaChain()
    _enable_media_recognize_share(chain)
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    fallback = MusicInfo(title="未知曲目", artists=["未知艺术家"])

    with patch.object(chain, "recognize_music_from_source", return_value=fallback), patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value=None,
    ) as query_mock, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share"
    ) as report_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is fallback
    query_mock.assert_called_once()
    report_mock.assert_not_called()
