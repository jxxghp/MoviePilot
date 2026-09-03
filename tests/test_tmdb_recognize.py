# -*- coding: utf-8 -*-
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from app.adapters.external.server import MoviePilotServerHelper
from app.chain.base import ChainBase
from app.domain.metainfo import MetaInfo
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.schemas.types import MediaSource, MediaType

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
            f"TMDB cassette 未命中：{key}；如识别流程新增请求，请重新录制 tests/fixtures/tmdb_recognize_cassette.json"
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
    _PATCHERS.extend(
        [
            patch.object(TMDb, "request", _replay_request),
            patch.object(TMDb, "async_request", _replay_async_request),
            patch.object(MoviePilotServerHelper, "async_report_recognize_share", new=AsyncMock(return_value=None)),
            patch.object(MoviePilotServerHelper, "async_query_recognize_share", new=AsyncMock(return_value=None)),
            patch.object(MoviePilotServerHelper, "report_recognize_share", new=MagicMock(return_value=None)),
            patch.object(MoviePilotServerHelper, "query_recognize_share", new=MagicMock(return_value=None)),
        ]
    )
    started = []
    try:
        for patcher in _PATCHERS:
            patcher.start()
            started.append(patcher)
    except Exception:
        # 仅回滚已成功启动的桩：对未启动的 patcher 调用 stop() 会抛 RuntimeError，
        # 既掩盖原始启动异常又中断清理；记录 started 可精确回滚、避免半启动状态泄漏。
        for patcher in started:
            patcher.stop()
        _PATCHERS.clear()
        raise


def tearDownModule():
    """还原 TMDb HTTP 出口打桩，避免影响其它测试模块。"""
    for patcher in _PATCHERS:
        patcher.stop()
    _PATCHERS.clear()


def test_tmdb_module_has_no_legacy_category_runtime_dependency() -> None:
    """正常模块初始化不得构造旧分类门面或暴露旧模块调度方法。"""
    with (
        patch("app.modules.themoviedb.TmdbCache"),
        patch("app.modules.themoviedb.TmdbApi"),
        patch("app.modules.themoviedb.TmdbScraper"),
    ):
        module = TheMovieDbModule()
        module.init_module()

    assert not hasattr(module, "category")
    assert not hasattr(module, "_category_helper")
    assert not hasattr(module, "media_category")
    assert not hasattr(module, "load_category_config")
    assert not hasattr(module, "save_category_config")


@pytest.fixture
def tmdb_module():
    """创建并在用例结束后关闭独立的 TMDB 模块实例。"""
    module = TheMovieDbModule()
    module.init_module()
    yield module
    module.stop()


@pytest.fixture
def chain():
    """在 pytest 组合根完成装配后创建端到端 Chain。"""
    return ChainBase()


@pytest.mark.asyncio
async def test_tmdbid_priority_over_title(tmdb_module):
    """文件名带 TMDB 身份时优先按身份识别，不回退标题搜索。"""
    meta = MetaInfo(title="空之境界 {tmdbid=938416}")
    assert meta.media_source is MediaSource.TMDB
    assert meta.media_id == "938416"
    assert meta.cn_name == "空之境界"

    result = await tmdb_module.async_recognize_media(
        meta=meta,
        media_source=meta.media_source,
        media_id=meta.media_id,
        cache=False,
    )
    assert result is not None, "应能识别到媒体信息"
    assert result.tmdb_id == 938416


@pytest.mark.asyncio
async def test_tmdbid_disambiguation_tv_vs_movie(tmdb_module):
    """同一 TMDB ID 同时命中电影和剧集时按标题证据消歧。"""
    meta = MetaInfo(title="空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
    assert meta.media_source is MediaSource.TMDB
    assert meta.media_id == "23155"

    result = await tmdb_module.async_recognize_media(
        meta=meta,
        media_source=meta.media_source,
        media_id=meta.media_id,
        cache=False,
    )
    assert result is not None, "同 ID 存在电影和电视剧时应能通过元数据消歧"
    assert result.tmdb_id == 23155
    assert result.type is MediaType.MOVIE


@pytest.mark.asyncio
async def test_tmdbid_with_explicit_type(tmdb_module):
    """显式媒体类型与 TMDB 身份同时存在时直接查询指定类型。"""
    meta = MetaInfo(title="空之境界 {tmdbid=23155}")
    result = await tmdb_module.async_recognize_media(
        meta=meta,
        media_source=meta.media_source,
        media_id=meta.media_id,
        mtype=MediaType.TV,
        cache=False,
    )
    assert result is not None
    assert result.tmdb_id == 23155
    assert result.type is MediaType.TV


@pytest.mark.asyncio
async def test_tmdbid_only_movie_exists(tmdb_module):
    """只有电影记录时忽略标题对 TV 类型的错误推断。"""
    meta = MetaInfo(title="少女与战车 最终章 ～第2话～ (2019) {tmdbid=496891}")
    assert meta.media_source is MediaSource.TMDB
    assert meta.media_id == "496891"

    result = await tmdb_module.async_recognize_media(
        meta=meta,
        media_source=meta.media_source,
        media_id=meta.media_id,
        cache=False,
    )
    assert result is not None, "仅存在电影时应正确识别"
    assert result.tmdb_id == 496891
    assert result.type is MediaType.MOVIE


@pytest.mark.asyncio
async def test_chain_tmdbid_movie(chain):
    """端到端 Chain 应按 TMDB 身份识别电影。"""
    meta = MetaInfo(title="空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
    result = await chain.async_recognize_media(meta=meta, cache=False)
    assert result is not None
    assert result.tmdb_id == 23155
    assert result.type is MediaType.MOVIE


@pytest.mark.asyncio
async def test_chain_tmdbid_ignores_inferred_type(chain):
    """端到端 Chain 有 TMDB 身份时不采信标题误推断的类型。"""
    meta = MetaInfo(title="少女与战车 最终章 ～第2话～ (2019) {tmdbid=496891}")
    assert meta.type is MediaType.TV, "meta.type 应被推断为 TV"
    assert meta.media_source is MediaSource.TMDB
    assert meta.media_id == "496891"

    result = await chain.async_recognize_media(meta=meta, cache=False)
    assert result is not None, "有 TMDB 身份时不应因类型推断错误而识别失败"
    assert result.tmdb_id == 496891
    assert result.type is MediaType.MOVIE


@pytest.mark.asyncio
async def test_chain_no_tmdbid_uses_inferred_type(chain):
    """无 TMDB 身份时端到端 Chain 继续使用标题推断类型。"""
    meta = MetaInfo(title="进击的巨人 S01E01")
    assert meta.type is MediaType.TV

    result = await chain.async_recognize_media(meta=meta, cache=False)
    assert result is not None
    assert result.type is MediaType.TV
