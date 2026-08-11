"""共享媒体识别链路测试。

覆盖本地识别成功后上报共享识别、本地识别失败后回查共享识别并二次识别、
共享识别成功后回填本地缓存、音乐识别上报/查询载荷，以及命中缓存不重复上报等场景。
"""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.core.context import MediaInfo, MusicInfo
from app.core.meta import MetaBase, MetaMusic
from app.core.metainfo import MetaInfo
from app.helper.server import MoviePilotServerHelper
from app.schemas.types import MediaType


def _build_meta(name: str, media_type: MediaType = MediaType.UNKNOWN) -> MetaBase:
    """构造测试用元数据。"""
    meta = MetaBase(name)
    meta.name = name
    meta.type = media_type
    return meta


def test_report_shared_result_after_local_recognize_success():
    """本地识别成功后应上报共享识别结果。"""
    chain = ChainBase()
    meta = _build_meta("测试电影", MediaType.MOVIE)
    mediainfo = MediaInfo(title="测试电影", year="2024", tmdb_id=100, type=MediaType.MOVIE)

    with patch.object(chain, "run_module", return_value=mediainfo) as run_module, patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is mediainfo
    run_module.assert_called_once()
    report_mock.assert_called_once_with(meta=meta, mediainfo=mediainfo, keyword_meta=meta)
    query_mock.assert_not_called()


def test_query_shared_result_when_local_recognize_failed():
    """本地识别失败后应回查共享识别结果，并按共享ID再次识别。"""
    chain = ChainBase()
    meta = _build_meta("测试剧集")
    shared_media = MediaInfo(title="测试剧集", year="2024", tmdb_id=200, type=MediaType.TV)

    with patch.object(
        chain,
        "run_module",
        side_effect=[None, shared_media],
    ) as run_module, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value={"type": "tv", "tmdbid": 200, "season": 1},
    ) as query_mock, patch(
        "app.chain.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.TV,
            "tmdbid": 200,
            "doubanid": None,
            "bangumiid": None,
            "season": 1,
        },
    ), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is shared_media
    assert run_module.call_count == 2
    query_mock.assert_called_once_with(meta=meta, mtype=None, keyword_meta=meta)
    second_call = run_module.call_args_list[1]
    assert second_call.kwargs["tmdbid"] == 200
    assert second_call.kwargs["mtype"] == MediaType.TV
    assert meta.begin_season is None


def test_async_query_shared_result_when_local_recognize_failed():
    """异步识别失败后也应回查共享识别结果。"""
    chain = ChainBase()
    meta = _build_meta("测试异步剧集")
    shared_media = MediaInfo(title="测试异步剧集", year="2025", tmdb_id=300, type=MediaType.TV)
    async_run_module = AsyncMock(side_effect=[None, shared_media])

    async def runner():
        with patch.object(
            chain,
            "async_run_module",
            async_run_module,
        ), patch(
            "app.chain.MoviePilotServerHelper.async_query_recognize_share",
            AsyncMock(return_value={"type": "tv", "tmdbid": 300, "season": 2}),
        ) as query_mock, patch(
            "app.chain.MoviePilotServerHelper.to_recognize_params",
            return_value={
                "mtype": MediaType.TV,
                "tmdbid": 300,
                "doubanid": None,
                "bangumiid": None,
                "season": 2,
            },
        ), patch(
            "app.chain.MoviePilotServerHelper.async_report_recognize_share",
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
    assert async_run_module.await_count == 2
    query_mock.assert_awaited_once_with(meta=meta, mtype=None, keyword_meta=meta)
    backfill_mock.assert_awaited_once()
    assert meta.begin_season is None


def test_backfill_local_cache_after_shared_recognize_success():
    """共享识别后二次本地识别成功时，应回填原始名称对应的本地识别缓存。"""
    chain = ChainBase()
    meta = _build_meta("测试缓存回填", MediaType.MOVIE)
    shared_media = MediaInfo(
        title="测试缓存回填",
        year="2024",
        tmdb_id=700,
        type=MediaType.MOVIE,
        source="themoviedb",
        tmdb_info={"id": 700, "media_type": MediaType.MOVIE, "title": "测试缓存回填"},
    )

    with patch.object(
        chain,
        "run_module",
        side_effect=[None, shared_media, None],
    ) as run_module_mock, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value={"type": "movie", "tmdbid": 700},
    ), patch(
        "app.chain.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MOVIE,
            "tmdbid": 700,
            "doubanid": None,
            "bangumiid": None,
            "season": None,
        },
    ), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is shared_media
    assert run_module_mock.call_count == 3
    update_call = run_module_mock.call_args_list[2]
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
    mediainfo = MediaInfo(
        title="测试剧集",
        year="2024",
        tmdb_id=400,
        type=MediaType.TV,
        season=1,
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

    mediainfo = MediaInfo(
        title="测试剧集",
        year="2024",
        tmdb_id=401,
        type=MediaType.TV,
        season=2,
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
    mediainfo = MediaInfo(title="测试剧", tmdb_id=402, type=MediaType.TV, season=0)

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
    mediainfo = MediaInfo(title="测试剧集", year="2024", tmdb_id=402, type=MediaType.TV)

    with patch.object(chain, "run_module", return_value=mediainfo), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
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
    meta = _build_meta("辅助识别后的名称", MediaType.TV)
    meta.year = "2024"
    share_meta = _build_meta("辅助识别前的名称", MediaType.UNKNOWN)
    share_meta.original_name = "辅助识别前的名称"
    shared_media = MediaInfo(title="测试剧集", year="2024", tmdb_id=403, type=MediaType.TV)

    with patch.object(
        chain,
        "run_module",
        side_effect=[None, shared_media],
    ), patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value={"type": "tv", "tmdbid": 403, "season": 1},
    ) as query_mock, patch(
        "app.chain.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.TV,
            "tmdbid": 403,
            "doubanid": None,
            "bangumiid": None,
            "season": 1,
        },
    ), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
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
    mediainfo = MediaInfo(title="缓存电影", year="2024", tmdb_id=500, type=MediaType.MOVIE)
    mediainfo.recognize_cache_hit = True

    with patch.object(chain, "run_module", return_value=mediainfo) as run_module, patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta)

    assert result is mediainfo
    run_module.assert_called_once()
    report_mock.assert_not_called()
    query_mock.assert_not_called()


def test_async_skip_report_when_local_recognize_hits_cache():
    """异步本地识别命中缓存时不应上报共享识别。"""
    chain = ChainBase()
    meta = _build_meta("缓存剧集", MediaType.TV)
    mediainfo = MediaInfo(title="缓存剧集", year="2025", tmdb_id=600, type=MediaType.TV)
    mediainfo.recognize_cache_hit = True

    async def runner():
        with patch.object(
            chain,
            "async_run_module",
            AsyncMock(return_value=mediainfo),
        ) as async_run_module, patch(
            "app.chain.MoviePilotServerHelper.async_report_recognize_share",
            AsyncMock(return_value=True),
        ) as report_mock, patch(
            "app.chain.MoviePilotServerHelper.async_query_recognize_share",
            AsyncMock(),
        ) as query_mock:
            result = await chain.async_recognize_media(meta=meta)
        return result, async_run_module, report_mock, query_mock

    result, async_run_module, report_mock, query_mock = asyncio.run(runner())

    assert result is mediainfo
    async_run_module.assert_awaited_once()
    report_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


def test_recognize_by_meta_can_skip_obtain_images():
    """标题识别可显式关闭图片拉取。"""
    media_chain = MediaChain()
    meta = MetaInfo("测试电影")
    mediainfo = MediaInfo(title="测试电影", year="2024", tmdb_id=404, type=MediaType.MOVIE)

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
    plugin_media = MediaInfo(title="辅助后名称", year="2024", tmdb_id=405, type=MediaType.TV)

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
    mediainfo = MediaInfo(title="测试异步电影", year="2025", tmdb_id=406, type=MediaType.MOVIE)

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
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def test_music_report_payload_includes_source_and_media_id():
    """音乐上报载荷应携带 music 类型与数据源原生身份，其余媒体 ID 恒为 None。"""
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
    assert payload["tmdbid"] is None
    assert payload["doubanid"] is None
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
    assert params["source"] == "musicbrainz"
    assert params["mediaid"] == "release-group-1"
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

    with patch("app.chain.music.MusicChain.recognize_best", return_value=music), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ) as report_mock, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share"
    ) as query_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    report_mock.assert_called_once_with(meta=meta, mediainfo=music, keyword_meta=meta)
    query_mock.assert_not_called()


def test_chain_recognize_media_queries_music_share_when_local_failed():
    """音乐本地识别失败后应回查共享识别并按数据源原生 ID 二次识别。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    music = _music_info()

    with patch(
        "app.chain.music.MusicChain.recognize_best",
        return_value=None,
    ) as recognize_best, patch(
        "app.chain.music.MusicChain.recognize_from_source",
        return_value=music,
    ) as recognize_source, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "music",
            "media_source": "musicbrainz",
            "media_id": "recording-1",
            "music_type": "recording",
        },
    ), patch(
        "app.chain.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MUSIC,
            "source": "musicbrainz",
            "mediaid": "recording-1",
            "music_type": "recording",
            "tmdbid": None,
            "doubanid": None,
            "bangumiid": None,
            "anilistid": None,
            "season": None,
        },
    ), patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    recognize_best.assert_called_once_with(meta=meta, cache=False)
    recognize_source.assert_called_once_with(
        source="musicbrainz",
        meta=meta,
        mediaid="recording-1",
        cache=False,
        music_type="recording",
    )


def test_chain_recognize_media_queries_music_share_after_local_fallback():
    """本地标签兜底没有远端身份时，仍应通过共享结果补成标准音乐身份。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = MusicInfo(title="晴天", artists=["周杰伦"])
    music = _music_info()

    with patch(
        "app.chain.music.MusicChain.recognize_best",
        return_value=fallback,
    ), patch(
        "app.chain.music.MusicChain.recognize_from_source",
        return_value=music,
    ) as recognize_source, patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value={
            "type": "music",
            "media_source": "musicbrainz",
            "media_id": "recording-1",
            "music_type": "recording",
        },
    ) as query_share, patch(
        "app.chain.MoviePilotServerHelper.to_recognize_params",
        return_value={
            "mtype": MediaType.MUSIC,
            "source": "musicbrainz",
            "mediaid": "recording-1",
            "music_type": "recording",
            "tmdbid": None,
            "doubanid": None,
            "bangumiid": None,
            "anilistid": None,
            "season": None,
        },
    ), patch.object(
        chain,
        "_update_local_recognize_cache",
    ), patch(
        "app.chain.settings.MEDIA_RECOGNIZE_SHARE",
        True,
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is music
    query_share.assert_called_once_with(
        meta=meta,
        mtype=MediaType.MUSIC,
        keyword_meta=meta,
    )
    recognize_source.assert_called_once()


def test_chain_async_recognize_media_queries_music_share_after_local_fallback():
    """异步音乐识别也必须在返回本地兜底前尝试共享身份补全。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = MusicInfo(title="晴天", artists=["周杰伦"])
    music = _music_info()

    async def runner():
        with patch(
            "app.chain.music.MusicChain.async_recognize_best",
            new=AsyncMock(return_value=fallback),
        ), patch(
            "app.chain.music.MusicChain.async_recognize_from_source",
            new=AsyncMock(return_value=music),
        ) as recognize_source, patch(
            "app.chain.MoviePilotServerHelper.async_query_recognize_share",
            new=AsyncMock(return_value={
                "type": "music",
                "media_source": "musicbrainz",
                "media_id": "recording-1",
                "music_type": "recording",
            }),
        ) as query_share, patch(
            "app.chain.MoviePilotServerHelper.to_recognize_params",
            return_value={
                "mtype": MediaType.MUSIC,
                "source": "musicbrainz",
                "mediaid": "recording-1",
                "music_type": "recording",
                "tmdbid": None,
                "doubanid": None,
                "bangumiid": None,
                "anilistid": None,
                "season": None,
            },
        ), patch.object(
            chain,
            "_async_update_local_recognize_cache",
            new=AsyncMock(),
        ), patch(
            "app.chain.settings.MEDIA_RECOGNIZE_SHARE",
            True,
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
    recognize_source.assert_awaited_once_with(
        source="musicbrainz",
        meta=meta,
        mediaid="recording-1",
        cache=False,
        music_type="recording",
    )


def test_chain_recognize_media_skips_music_report_for_fallback_result():
    """共享也未命中时保留音乐标签兜底，且不把无身份结果上报。"""
    chain = MediaChain()
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    fallback = MusicInfo(title="未知曲目", artists=["未知艺术家"])

    with patch("app.chain.music.MusicChain.recognize_best", return_value=fallback), patch(
        "app.chain.MoviePilotServerHelper.query_recognize_share",
        return_value=None,
    ) as query_mock, patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share"
    ) as report_mock, patch(
        "app.chain.settings.MEDIA_RECOGNIZE_SHARE", True
    ):
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is fallback
    query_mock.assert_called_once()
    report_mock.assert_not_called()
