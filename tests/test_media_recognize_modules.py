import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.scraper import TmdbScraper
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.schemas.types import MediaType


class MediaRecognizeModulesTest(TestCase):
    def test_tmdb_cache_false_skips_cache_lookup(self):
        """cache=False 时应跳过缓存读取，但仍按正常流程查询 TMDB。"""
        module = TheMovieDbModule()
        meta = MetaBase("测试电影")
        meta.name = "测试电影"
        meta.type = MediaType.MOVIE
        module.cache = Mock()
        module.tmdb = Mock()
        module.tmdb.get_info.return_value = {
            "id": 100,
            "media_type": MediaType.MOVIE,
            "title": "测试电影",
            "genres": [],
        }
        module.category = Mock()
        module.category.get_movie_category.return_value = None

        result = module.recognize_media(meta=meta, tmdbid=100, cache=False)

        self.assertIsInstance(result, MediaInfo)
        self.assertEqual(result.tmdb_id, 100)
        module.cache.get.assert_not_called()
        module.cache.update.assert_called_once()

    def test_async_tmdb_cache_false_skips_cache_lookup(self):
        """异步 cache=False 时也应跳过缓存读取。"""
        module = TheMovieDbModule()
        meta = MetaBase("测试电影")
        meta.name = "测试电影"
        meta.type = MediaType.MOVIE
        module.cache = Mock()
        module.tmdb = Mock()

        async def _async_get_info(**kwargs):
            return {
                "id": 101,
                "media_type": MediaType.MOVIE,
                "title": "测试电影",
                "genres": [],
            }

        module.tmdb.async_get_info = _async_get_info
        module.category = Mock()
        module.category.get_movie_category.return_value = None

        result = asyncio.run(module.async_recognize_media(meta=meta, tmdbid=101, cache=False))

        self.assertIsInstance(result, MediaInfo)
        self.assertEqual(result.tmdb_id, 101)
        module.cache.get.assert_not_called()
        module.cache.update.assert_called_once()

    def test_tmdb_recognize_does_not_fallback_to_match_web(self):
        """TMDB API 搜索无结果时，不应再回退抓取 TMDB 网站搜索页。"""
        module = TheMovieDbModule()
        meta = MetaBase("No Match Movie")
        meta.name = "No Match Movie"
        meta.type = MediaType.MOVIE
        module.cache = Mock()
        module.tmdb = Mock()
        module.tmdb.match_web.side_effect = AssertionError("不应调用 TMDB 网站搜索")
        module._search_by_name = Mock(return_value=None)

        result = module.recognize_media(meta=meta, cache=False)

        self.assertIsNone(result)
        module._search_by_name.assert_called()
        module.tmdb.match_web.assert_not_called()

    def test_async_tmdb_recognize_does_not_fallback_to_match_web(self):
        """异步 TMDB API 搜索无结果时，不应再回退抓取 TMDB 网站搜索页。"""
        module = TheMovieDbModule()
        meta = MetaBase("No Match Movie")
        meta.name = "No Match Movie"
        meta.type = MediaType.MOVIE
        module.cache = Mock()
        module.tmdb = Mock()
        module.tmdb.async_match_web = AsyncMock(side_effect=AssertionError("不应调用 TMDB 网站搜索"))
        module._async_search_by_name = AsyncMock(return_value=None)

        result = asyncio.run(module.async_recognize_media(meta=meta, cache=False))

        self.assertIsNone(result)
        module._async_search_by_name.assert_called()
        module.tmdb.async_match_web.assert_not_called()

    def test_tmdb_image_language_fallback_includes_current_en_null_and_original(self):
        """TMDB 图片查询应带上语言回退，避免当前语言没有图片时直接返回空。"""
        with patch("app.modules.themoviedb.tmdbapi.settings") as mock_settings:
            mock_settings.TMDB_LOCALE = "zh"

            result = TmdbApi._build_include_image_language("ja")

        self.assertEqual(result, "zh,en,null,ja")

    def test_tmdb_trending_filters_non_media_and_normalizes_media_type(self):
        """TMDB流行趋势应过滤人物项，并把字符串媒体类型转为内部枚举。"""
        infos = [
            {"id": 100, "media_type": "movie", "title": "测试电影"},
            {"id": 101, "media_type": "tv", "name": "测试剧集"},
            {"id": 102, "media_type": "person", "name": "测试人物"},
            {"id": 103, "media_type": MediaType.MOVIE, "title": "枚举电影"},
        ]

        result = TmdbApi._normalize_trending_infos(infos)

        self.assertEqual([info["id"] for info in result], [100, 101, 103])
        self.assertEqual(result[0]["media_type"], MediaType.MOVIE)
        self.assertEqual(result[1]["media_type"], MediaType.TV)
        self.assertEqual(result[2]["media_type"], MediaType.MOVIE)

    def test_tmdb_obtain_images_uses_language_fallback_and_picks_best(self):
        """obtain_images 应从图片接口回填缺失的海报和背景图。"""
        module = TheMovieDbModule()
        module.tmdb = Mock()
        module.tmdb.get_movie_images.return_value = {
            "posters": [
                {"file_path": "/low-poster.jpg", "vote_average": 2, "vote_count": 10},
                {"file_path": "/best-poster.jpg", "vote_average": 8, "vote_count": 1},
            ],
            "backdrops": [
                {"file_path": "/best-backdrop.jpg", "vote_average": 7, "vote_count": 2},
            ],
        }
        mediainfo = MediaInfo(
            tmdb_id=100,
            type=MediaType.MOVIE,
            original_language="ja",
        )

        result = module.obtain_images(mediainfo)

        self.assertIs(result, mediainfo)
        module.tmdb.get_movie_images.assert_called_once_with(100, original_language="ja")
        self.assertTrue(mediainfo.poster_path.endswith("/best-poster.jpg"))
        self.assertTrue(mediainfo.backdrop_path.endswith("/best-backdrop.jpg"))

    def test_tmdb_scraper_metadata_img_fetches_missing_main_images(self):
        """主媒体图片缺失时，刮削图片列表应先从 TMDB images 接口补齐。"""
        scraper = TmdbScraper()
        scraper._meta_tmdb = Mock()
        scraper._meta_tmdb.get_movie_images.return_value = {
            "posters": [
                {"file_path": "/fallback-poster.jpg", "vote_average": 5},
            ],
            "backdrops": [
                {"file_path": "/fallback-backdrop.jpg", "vote_average": 4},
            ],
        }
        mediainfo = MediaInfo(
            tmdb_id=200,
            type=MediaType.MOVIE,
            original_language="en",
        )

        images = scraper.get_metadata_img(mediainfo)

        scraper._meta_tmdb.get_movie_images.assert_called_once_with(
            200,
            original_language="en",
        )
        self.assertIn("poster.jpg", images)
        self.assertIn("backdrop.jpg", images)

    def test_douban_prepare_search_names_deduplicates_simplified_name(self):
        """豆瓣候选名称应保留顺序，并去掉繁简转换后的重复项。"""
        meta = MetaBase("流浪地球")
        meta.cn_name = "流浪地球"
        meta.en_name = "The Wandering Earth"

        self.assertEqual(
            DoubanModule._prepare_search_names(meta),
            ["流浪地球", "The Wandering Earth"],
        )

    def test_douban_search_result_helper_preserves_season_title_rule(self):
        """豆瓣搜索结果 helper 应保留电视剧标题追加季号的旧逻辑。"""
        meta = MetaBase("测试剧")
        meta.name = "测试剧"
        meta.type = MediaType.TV
        meta.begin_season = 2
        items = [
            {
                "type_name": MediaType.TV.value,
                "target": {
                    "id": "200",
                    "title": "测试剧",
                    "type": "tv",
                    "year": "2024",
                },
            },
            {
                "type_name": MediaType.MOVIE.value,
                "target": {
                    "id": "201",
                    "title": "测试剧 电影版",
                    "type": "movie",
                    "year": "2024",
                },
            },
        ]

        result = DoubanModule._build_search_medias_result(meta, items)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "测试剧 第二季")
        self.assertEqual(result[0].season, 2)

    def test_douban_process_search_results_preserves_special_season_zero(self):
        """豆瓣搜索匹配应把 season=0 当作明确季号，而不是默认回第 1 季。"""
        result = {
            "items": [
                {
                    "type_name": MediaType.TV.value,
                    "target": {
                        "id": "200",
                        "title": "测试剧 S01",
                        "type": "tv",
                        "year": "2024",
                    },
                },
                {
                    "type_name": MediaType.TV.value,
                    "target": {
                        "id": "201",
                        "title": "测试剧 S00",
                        "type": "tv",
                        "year": "2024",
                    },
                },
            ]
        }

        matched = DoubanModule._process_search_results(
            result,
            "测试剧",
            mtype=MediaType.TV,
            year="2024",
            season=0,
        )

        self.assertEqual(matched["id"], "201")
