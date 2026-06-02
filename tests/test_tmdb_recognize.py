# -*- coding: utf-8 -*-
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.core.metainfo import MetaInfo
from app.chain import ChainBase
from app.helper.server import MoviePilotServerHelper
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.schemas.types import MediaType

# 离线 TMDB 响应回放：识别测试断言的是 tmdbid 优先/电影电视消歧/类型推断等逻辑，
# 这些逻辑需要真实结构的 TMDB 响应才有意义，但直连 api.themoviedb.org 属于不可接受的
# 外部 IO（CI 冷缓存下单文件 ~75s 且 flaky）。这里用一次性录制的真实响应 cassette 回放
# TMDb 的 HTTP 出入口，既保持识别逻辑被真实数据驱动，又彻底离线。重新录制见提交说明。
_CASSETTE_PATH = Path(__file__).resolve().parent / "fixtures" / "tmdb_recognize_cassette.json"
_CASSETTE: dict = json.loads(_CASSETTE_PATH.read_text(encoding="utf-8"))
# 响应快照标记键，与 TMDb._snapshot_response 写入的结构保持一致
_MARKER = TMDb._RESPONSE_SNAPSHOT_MARKER


def _cassette_key(url: str) -> str:
    """把 TMDB 请求 URL 归一化为 cassette 键：剥离易变的 api_key，其余 query 排序。

    `_build_url` 生成形如 `/3/movie/23155?api_key=...&append_to_response=...&language=zh`，
    剥离 api_key 后键在不同环境/不同 key 下保持稳定。
    """
    parts = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "api_key")
    return f"{parts.path}?{urlencode(query)}"


def _replay(url: str) -> dict:
    """按归一化键回放录制的响应快照；未命中即报错提示重新录制，避免静默漏过新请求。"""
    key = _cassette_key(url)
    if key not in _CASSETTE:
        raise AssertionError(
            f"TMDB cassette 未命中：{key}；如识别流程新增请求，请重新录制 "
            f"tests/fixtures/tmdb_recognize_cassette.json"
        )
    # headers 置空：识别只消费 json，丢弃录制头可规避限流/ETag 等无关分支
    return {_MARKER: True, "headers": {}, "json": deepcopy(_CASSETTE[key])}


def _replay_request(self, method, url, data, json=None, **kwargs):  # noqa: A002 - 对齐被替换方法签名
    """TMDb.request 的离线替身（同步）。"""
    return _replay(url)


async def _replay_async_request(self, method, url, data, json=None, **kwargs):  # noqa: A002 - 同上
    """TMDb.async_request 的离线替身（异步）。"""
    return _replay(url)


_PATCHERS: list = []


def setUpModule():
    """整文件生效：离线化 TMDB HTTP 与共享识别 API，确保零真实请求。

    ChainBase.async_recognize_media 在识别成功后会经 MoviePilotServerHelper 向
    MP 服务器（movie-pilot.org）的「共享识别 API」上报/查询；识别失败时还会反向
    查询。这两条链路与 TMDB 目录无关，必须一并打桩，否则 Chain 端到端用例仍会真发请求。
    """
    _PATCHERS.extend([
        patch.object(TMDb, "request", _replay_request),
        patch.object(TMDb, "async_request", _replay_async_request),
        patch.object(MoviePilotServerHelper, "async_report_recognize_share", new=AsyncMock(return_value=None)),
        patch.object(MoviePilotServerHelper, "async_query_recognize_share", new=AsyncMock(return_value=None)),
        patch.object(MoviePilotServerHelper, "report_recognize_share", new=MagicMock(return_value=None)),
        patch.object(MoviePilotServerHelper, "query_recognize_share", new=MagicMock(return_value=None)),
    ])
    for patcher in _PATCHERS:
        patcher.start()


def tearDownModule():
    """还原 TMDb HTTP 出口打桩，避免影响其它测试模块。"""
    for patcher in _PATCHERS:
        patcher.stop()
    _PATCHERS.clear()


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
