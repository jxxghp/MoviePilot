"""订阅换词策略减少重复等待和 IMDb 请求，同时保留普通搜索行为。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.subscription.sitebudget import SubscriptionSiteBudget
from app.chain.search import SearchChain, execution, media
from app.domain.context import MediaInfo
from app.schemas.types import MediaType


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("subscription", [False, True])
@pytest.mark.parametrize("area,imdb_id", [("imdbid", "tt1234567"), ("imdbid", None), ("title", "tt1234567")])
def test_subscription_queries_skip_round_wait_and_duplicate_imdb_requests(
    monkeypatch, mode, subscription, area, imdb_id,
):
    """真实执行循环在无命中时也不重复 IMDb 请求，缺 ID 和普通搜索仍完整换词。"""
    chain = object.__new__(SearchChain)
    chain.runtime_config = SimpleNamespace(search_multiple_name=True)
    aliases = ["Main Title", "Original Title", "Another Title"]
    target = MediaInfo(tmdb_id=1, title=aliases[0], names=aliases, imdb_id=imdb_id, type=MediaType.MOVIE)
    if subscription:
        chain.configure_subscription_site_budget(SubscriptionSiteBudget(
            repository=Mock(), owner="test-keywords", cancelled=lambda: False,
            stop_state=SimpleNamespace(is_system_stopped=False),
        ))
    queries = []
    sleeps = []

    def search(**kwargs):
        """在站点 I/O 边界记录最终检索词，所有查询都返回空结果。"""
        queries.append(chain._torrent_keyword(kwargs["keyword"], kwargs["mediainfo"], kwargs["area"]))
        return []

    async def async_search(**kwargs):
        """异步查询使用同一站点边界替身。"""
        return search(**kwargs)

    async def stream(**kwargs):
        """流式查询发布与同步查询相同的空页。"""
        yield {"items": search(**kwargs)}

    async def supplement(mediainfo):
        """隔离外部媒体补充请求。"""
        return mediainfo

    async def sleep(delay):
        """记录异步等待时间而不让测试实际睡眠。"""
        sleeps.append(delay)

    monkeypatch.setattr(media, "MediaChain", lambda: SimpleNamespace(
        supplement_media_info=lambda mediainfo: mediainfo, async_supplement_media_info=supplement,
    ))
    monkeypatch.setattr(execution.random, "randint", lambda _start, _end: 5)
    monkeypatch.setattr(execution.time, "sleep", sleeps.append)
    monkeypatch.setattr(execution.asyncio, "sleep", sleep)
    chain._prepare_params = lambda **_kwargs: (None, aliases)
    chain._parse_result = Mock(return_value=[])
    chain._SearchChain__search_all_sites = search
    chain._SearchChain__async_search_all_sites = async_search
    chain._SearchChain__async_search_all_sites_stream = stream
    if mode == "sync":
        result = chain.process(target, area=area)
    elif mode == "async":
        result = asyncio.run(chain.async_process(target, area=area))
    else:
        async def collect():
            """从流式完成事件读取最终搜索结果。"""
            return [event async for event in chain.async_process_stream(target, area=area)]

        result = asyncio.run(collect())[-1]["contexts"]

    if area == "imdbid" and imdb_id:
        assert queries == [imdb_id] * (1 if subscription else len(aliases))
    else:
        assert queries == aliases
    assert sleeps == ([] if subscription else [5] * (len(queries) - 1))
    assert result == []
    chain._parse_result.assert_called_once()
