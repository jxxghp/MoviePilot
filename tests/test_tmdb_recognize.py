# -*- coding: utf-8 -*-
import asyncio
from unittest import TestCase

from app.core.metainfo import MetaInfo
from app.chain import ChainBase
from app.modules.themoviedb import TheMovieDbModule
from app.schemas.types import MediaType


class TmdbRecognizeModuleTest(TestCase):
    """
    TMDB模块层识别测试
    模块层的 async_recognize_media 不会自动从 meta.tmdbid 提取 tmdbid，
    该提取在 ChainBase 层完成，因此测试中需显式传入 tmdbid 参数。
    """

    @classmethod
    def setUpClass(cls):
        cls.module = TheMovieDbModule()
        cls.module.init_module()

    @classmethod
    def tearDownClass(cls):
        cls.module.stop()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_tmdbid_priority_over_title(self):
        """
        当标题中包含 {tmdbid=xxx} 时，应优先使用tmdbid识别，
        而非回退到标题搜索
        """
        meta = MetaInfo(title="空之境界 {tmdbid=938416}")
        self.assertEqual(meta.tmdbid, 938416)
        self.assertEqual(meta.cn_name, "空之境界")

        result = self._run(
            self.module.async_recognize_media(
                meta=meta, tmdbid=meta.tmdbid, cache=False
            )
        )
        self.assertIsNotNone(result, "应能识别到媒体信息")
        self.assertEqual(result.tmdb_id, 938416)

    def test_tmdbid_disambiguation_tv_vs_movie(self):
        """
        当同一tmdbid同时存在电影和电视剧时，应通过元数据消歧
        tmdbid=23155 同时存在电影"空之境界 第五章 矛盾螺旋"和电视剧"TV Land Top 10"
        标题包含"空之境界"应消歧为电影
        """
        meta = MetaInfo(title="空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
        self.assertEqual(meta.tmdbid, 23155)

        result = self._run(
            self.module.async_recognize_media(
                meta=meta, tmdbid=meta.tmdbid, cache=False
            )
        )
        self.assertIsNotNone(result, "同ID存在电影和电视剧时应能通过元数据消歧")
        self.assertEqual(result.tmdb_id, 23155)
        self.assertEqual(result.type, MediaType.MOVIE)

    def test_tmdbid_with_explicit_type(self):
        """
        当标题中同时包含 tmdbid 和 type 时，应直接使用指定类型查询
        """
        meta = MetaInfo(title="空之境界 {tmdbid=23155}")

        result = self._run(
            self.module.async_recognize_media(
                meta=meta, tmdbid=meta.tmdbid, mtype=MediaType.TV, cache=False
            )
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.tmdb_id, 23155)
        self.assertEqual(result.type, MediaType.TV)

    def test_tmdbid_only_movie_exists(self):
        """
        tmdbid仅存在电影时，即使meta.type推断为TV也应正确识别为电影
        tmdbid=496891 仅存在电影"少女与战车 最终章 ～第2话～"
        """
        meta = MetaInfo(title="少女与战车 最终章 ～第2话～ (2019) {tmdbid=496891}")
        self.assertEqual(meta.tmdbid, 496891)

        result = self._run(
            self.module.async_recognize_media(
                meta=meta, tmdbid=meta.tmdbid, cache=False
            )
        )
        self.assertIsNotNone(result, "仅存在电影时应正确识别")
        self.assertEqual(result.tmdb_id, 496891)
        self.assertEqual(result.type, MediaType.MOVIE)


class TmdbRecognizeChainTest(TestCase):
    """
    ChainBase层识别测试（端到端）
    验证从 meta.tmdbid 提取到模块识别的完整流程
    """

    @classmethod
    def setUpClass(cls):
        cls.chain = ChainBase()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_chain_tmdbid_movie(self):
        """
        通过ChainBase识别，tmdbid对应电影应正确识别
        """
        meta = MetaInfo(title="空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
        result = self._run(
            self.chain.async_recognize_media(meta=meta, cache=False)
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.tmdb_id, 23155)
        self.assertEqual(result.type, MediaType.MOVIE)

    def test_chain_tmdbid_ignores_inferred_type(self):
        """
        当tmdbid存在时，不应使用meta推断的类型
        "第2话"会让meta.type推断为TV，但tmdbid=496891仅存在电影
        """
        meta = MetaInfo(title="少女与战车 最终章 ～第2话～ (2019) {tmdbid=496891}")
        self.assertEqual(meta.type, MediaType.TV, "meta.type应被推断为TV")
        self.assertEqual(meta.tmdbid, 496891)

        result = self._run(
            self.chain.async_recognize_media(meta=meta, cache=False)
        )
        self.assertIsNotNone(result, "有tmdbid时不应因meta.type推断错误而识别失败")
        self.assertEqual(result.tmdb_id, 496891)
        self.assertEqual(result.type, MediaType.MOVIE)

    def test_chain_no_tmdbid_uses_inferred_type(self):
        """
        无tmdbid时，应正常使用meta推断的类型进行标题搜索
        """
        meta = MetaInfo(title="进击的巨人 S01E01")
        self.assertEqual(meta.type, MediaType.TV)

        result = self._run(
            self.chain.async_recognize_media(meta=meta, cache=False)
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.type, MediaType.TV)
