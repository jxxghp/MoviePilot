import asyncio
import sys
import unittest
from types import ModuleType
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))

from app.chain import ChainBase
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.chain.media import MediaChain
from app.helper.server import MoviePilotServerHelper
from app.schemas.types import MediaType


class TestMediaRecognizeShare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = ChainBase()
        cls.media_chain = MediaChain()

    @staticmethod
    def _build_meta(name: str, media_type: MediaType = MediaType.UNKNOWN) -> MetaBase:
        """
        构造测试用元数据
        """
        meta = MetaBase(name)
        meta.name = name
        meta.type = media_type
        return meta

    def test_report_shared_result_after_local_recognize_success(self):
        """
        本地识别成功后应上报共享识别结果
        """
        meta = self._build_meta("测试电影", MediaType.MOVIE)
        mediainfo = MediaInfo(title="测试电影", year="2024", tmdb_id=100, type=MediaType.MOVIE)

        with patch.object(self.chain, "run_module", return_value=mediainfo) as run_module, patch(
            "app.chain.MoviePilotServerHelper.report_recognize_share",
            return_value=True,
        ) as report_mock, patch(
            "app.chain.MoviePilotServerHelper.query_recognize_share"
        ) as query_mock:
            result = self.chain.recognize_media(meta=meta, cache=False)

        self.assertIs(result, mediainfo)
        run_module.assert_called_once()
        report_mock.assert_called_once_with(meta=meta, mediainfo=mediainfo, keyword_meta=meta)
        query_mock.assert_not_called()

    def test_query_shared_result_when_local_recognize_failed(self):
        """
        本地识别失败后应回查共享识别结果，并按共享ID再次识别
        """
        meta = self._build_meta("测试剧集")
        shared_media = MediaInfo(title="测试剧集", year="2024", tmdb_id=200, type=MediaType.TV)

        with patch.object(
            self.chain,
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
            self.chain,
            "_update_local_recognize_cache",
        ):
            result = self.chain.recognize_media(meta=meta, cache=False)

        self.assertIs(result, shared_media)
        self.assertEqual(run_module.call_count, 2)
        query_mock.assert_called_once_with(meta=meta, mtype=None, keyword_meta=meta)
        second_call = run_module.call_args_list[1]
        self.assertEqual(second_call.kwargs["tmdbid"], 200)
        self.assertEqual(second_call.kwargs["mtype"], MediaType.TV)
        self.assertIsNone(meta.begin_season)

    def test_async_query_shared_result_when_local_recognize_failed(self):
        """
        异步识别失败后也应回查共享识别结果
        """
        meta = self._build_meta("测试异步剧集")
        shared_media = MediaInfo(title="测试异步剧集", year="2025", tmdb_id=300, type=MediaType.TV)
        async_run_module = AsyncMock(side_effect=[None, shared_media])

        async def runner():
            with patch.object(
                self.chain,
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
                self.chain,
                "_async_update_local_recognize_cache",
                AsyncMock(),
            ) as backfill_mock:
                result = await self.chain.async_recognize_media(meta=meta, cache=False)
            return result, query_mock, backfill_mock

        result, query_mock, backfill_mock = asyncio.run(runner())

        self.assertIs(result, shared_media)
        self.assertEqual(async_run_module.await_count, 2)
        query_mock.assert_awaited_once_with(meta=meta, mtype=None, keyword_meta=meta)
        backfill_mock.assert_awaited_once()
        self.assertIsNone(meta.begin_season)

    def test_backfill_local_cache_after_shared_recognize_success(self):
        """
        共享识别后二次本地识别成功时，应回填原始名称对应的本地识别缓存。
        """
        meta = self._build_meta("测试缓存回填", MediaType.MOVIE)
        shared_media = MediaInfo(
            title="测试缓存回填",
            year="2024",
            tmdb_id=700,
            type=MediaType.MOVIE,
            source="themoviedb",
            tmdb_info={"id": 700, "media_type": MediaType.MOVIE, "title": "测试缓存回填"},
        )

        with patch.object(
            self.chain,
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
            result = self.chain.recognize_media(meta=meta, cache=False)

        self.assertIs(result, shared_media)
        self.assertEqual(run_module_mock.call_count, 3)
        update_call = run_module_mock.call_args_list[2]
        self.assertEqual(update_call.args[0], "update_recognize_cache")
        self.assertIsNot(update_call.kwargs["meta"], meta)
        self.assertEqual(update_call.kwargs["meta"].name, meta.name)
        self.assertEqual(update_call.kwargs["meta"].type, meta.type)
        self.assertIs(update_call.kwargs["mediainfo"], shared_media)

    def test_query_and_report_prefer_original_name_keyword(self):
        """
        查询和上报共享识别时应优先使用未应用识别词的识别名称
        """
        meta = self._build_meta("应用识别词后的名称", MediaType.TV)
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

        self.assertEqual(query_params["keyword"], "未应用识别词的名称")
        self.assertEqual(report_payload["keyword"], "未应用识别词的名称")

    def test_query_and_report_can_use_distinct_keyword_meta(self):
        """
        共享识别应允许用原始关键字上报，同时保留辅助识别后的年份/季信息。
        """
        meta = self._build_meta("辅助识别后的名称", MediaType.TV)
        meta.year = "2024"
        meta.begin_season = 2

        keyword_meta = self._build_meta("辅助识别前的名称", MediaType.UNKNOWN)
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

        self.assertEqual(query_params["keyword"], "辅助识别前的名称")
        self.assertEqual(query_params["year"], "2024")
        self.assertEqual(query_params["season"], 2)
        self.assertEqual(report_payload["keyword"], "辅助识别前的名称")
        self.assertEqual(report_payload["year"], "2024")
        self.assertEqual(report_payload["season"], 2)

    def test_report_shared_result_with_distinct_keyword_meta(self):
        """
        辅助识别成功后应按辅助前名称上报共享结果。
        """
        meta = self._build_meta("辅助识别后的名称", MediaType.TV)
        meta.year = "2024"
        meta.begin_season = 1
        share_meta = self._build_meta("辅助识别前的名称", MediaType.UNKNOWN)
        share_meta.original_name = "辅助识别前的名称"
        mediainfo = MediaInfo(title="测试剧集", year="2024", tmdb_id=402, type=MediaType.TV)

        with patch.object(self.chain, "run_module", return_value=mediainfo), patch(
            "app.chain.MoviePilotServerHelper.report_recognize_share",
            return_value=True,
        ) as report_mock:
            result = self.chain.recognize_media(meta=meta, share_meta=share_meta, cache=False)

        self.assertIs(result, mediainfo)
        report_mock.assert_called_once_with(
            meta=meta,
            mediainfo=mediainfo,
            keyword_meta=share_meta,
        )

    def test_query_shared_result_with_distinct_keyword_meta(self):
        """
        本地识别失败后应按辅助前名称回查共享结果。
        """
        meta = self._build_meta("辅助识别后的名称", MediaType.TV)
        meta.year = "2024"
        share_meta = self._build_meta("辅助识别前的名称", MediaType.UNKNOWN)
        share_meta.original_name = "辅助识别前的名称"
        shared_media = MediaInfo(title="测试剧集", year="2024", tmdb_id=403, type=MediaType.TV)

        with patch.object(
            self.chain,
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
            self.chain,
            "_update_local_recognize_cache",
        ):
            result = self.chain.recognize_media(
                meta=meta,
                share_meta=share_meta,
                cache=False,
            )

        self.assertIs(result, shared_media)
        query_mock.assert_called_once_with(
            meta=meta,
            mtype=MediaType.TV,
            keyword_meta=share_meta,
        )

    def test_skip_report_when_local_recognize_hits_cache(self):
        """
        本地识别命中缓存时不应上报共享识别
        """
        meta = self._build_meta("缓存电影", MediaType.MOVIE)
        mediainfo = MediaInfo(title="缓存电影", year="2024", tmdb_id=500, type=MediaType.MOVIE)
        mediainfo.recognize_cache_hit = True

        with patch.object(self.chain, "run_module", return_value=mediainfo) as run_module, patch(
            "app.chain.MoviePilotServerHelper.report_recognize_share",
            return_value=True,
        ) as report_mock, patch(
            "app.chain.MoviePilotServerHelper.query_recognize_share"
        ) as query_mock:
            result = self.chain.recognize_media(meta=meta)

        self.assertIs(result, mediainfo)
        run_module.assert_called_once()
        report_mock.assert_not_called()
        query_mock.assert_not_called()

    def test_async_skip_report_when_local_recognize_hits_cache(self):
        """
        异步本地识别命中缓存时不应上报共享识别
        """
        meta = self._build_meta("缓存剧集", MediaType.TV)
        mediainfo = MediaInfo(title="缓存剧集", year="2025", tmdb_id=600, type=MediaType.TV)
        mediainfo.recognize_cache_hit = True

        async def runner():
            with patch.object(
                self.chain,
                "async_run_module",
                AsyncMock(return_value=mediainfo),
            ) as async_run_module, patch(
                "app.chain.MoviePilotServerHelper.async_report_recognize_share",
                AsyncMock(return_value=True),
            ) as report_mock, patch(
                "app.chain.MoviePilotServerHelper.async_query_recognize_share",
                AsyncMock(),
            ) as query_mock:
                result = await self.chain.async_recognize_media(meta=meta)
            return result, async_run_module, report_mock, query_mock

        result, async_run_module, report_mock, query_mock = asyncio.run(runner())

        self.assertIs(result, mediainfo)
        async_run_module.assert_awaited_once()
        report_mock.assert_not_awaited()
        query_mock.assert_not_awaited()

    def test_recognize_by_meta_can_skip_obtain_images(self):
        """
        标题识别可显式关闭图片拉取。
        """
        meta = MetaInfo("测试电影")
        mediainfo = MediaInfo(title="测试电影", year="2024", tmdb_id=404, type=MediaType.MOVIE)

        with patch.object(
            self.media_chain,
            "recognize_media",
            return_value=mediainfo,
        ) as recognize_mock, patch.object(
            self.media_chain,
            "obtain_images",
        ) as obtain_images_mock:
            result = self.media_chain.recognize_by_meta(meta, obtain_images=False)

        self.assertIs(result, mediainfo)
        recognize_mock.assert_called_once()
        obtain_images_mock.assert_not_called()

    def test_recognize_by_meta_reports_with_original_keyword_after_plugin_help(self):
        """
        辅助识别后应继续使用辅助前关键字进行共享上报。
        """
        meta = MetaInfo("辅助前名称")
        plugin_media = MediaInfo(title="辅助后名称", year="2024", tmdb_id=405, type=MediaType.TV)

        with patch.object(
            self.media_chain,
            "select_recognize_source",
            side_effect=lambda **kwargs: kwargs["plugin_fn"](),
        ), patch.object(
            self.media_chain,
            "recognize_help",
            return_value=plugin_media,
        ) as recognize_help_mock, patch.object(
            self.media_chain,
            "obtain_images",
        ):
            result = self.media_chain.recognize_by_meta(meta, obtain_images=False)

        self.assertIs(result, plugin_media)
        self.assertEqual(recognize_help_mock.call_args.kwargs["share_meta"].name, "辅助前名称")

    def test_async_recognize_by_meta_can_skip_obtain_images(self):
        """
        异步标题识别可显式关闭图片拉取。
        """
        meta = MetaInfo("测试异步电影")
        mediainfo = MediaInfo(title="测试异步电影", year="2025", tmdb_id=406, type=MediaType.MOVIE)

        async def runner():
            with patch.object(
                self.media_chain,
                "async_recognize_media",
                AsyncMock(return_value=mediainfo),
            ) as recognize_mock, patch.object(
                self.media_chain,
                "async_obtain_images",
                AsyncMock(),
            ) as obtain_images_mock:
                result = await self.media_chain.async_recognize_by_meta(
                    meta,
                    obtain_images=False,
                )
            return result, recognize_mock, obtain_images_mock

        result, recognize_mock, obtain_images_mock = asyncio.run(runner())

        self.assertIs(result, mediainfo)
        recognize_mock.assert_awaited_once()
        obtain_images_mock.assert_not_awaited()
