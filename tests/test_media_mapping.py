"""
共享 TMDB 关系的转换顺序、缓存、故障降级和接口回归。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external import mapping as transport
from app.api.endpoints import media as endpoint
from app.application.server.mapping import configure_media_mapping_port
from app.chain.media import MediaChain
from app.runtime.loop import main_loop_registry
from app.schemas.mapping import MediaMapping, MediaMappingQuery
from app.schemas.types import MediaSource, MediaType


def query(**overrides):
    """
    提供来源季号与 TMDB 季号不同的动画查询上下文。
    """
    return MediaMappingQuery(media_source=MediaSource.AniList, media_id="123", type="tv", **overrides)


def relation(**overrides):
    """
    构造共享剧集第零季关系，确认不能把 0 当作空值。
    """
    return MediaMapping(**{**query().model_dump(), "tmdb_id": 456, "tmdb_type": "tv", "tmdb_season": 0, **overrides})


@pytest.fixture
def client(monkeypatch):
    """
    用受控传输隔离实际 Server，并允许检验请求次数。
    """
    config = {"MP_SERVER_HOST": "https://center.example", "MEDIA_RECOGNIZE_SHARE": True, "PROXY": None}
    monkeypatch.setattr(transport, "get_runtime_setting", config.get)
    return transport.MediaMappingClient()


def test_client_positive_negative_cache_expiry_and_configuration(client, monkeypatch):
    """
    正负缓存、类型季号和中心地址隔离，并在有效期后重新请求。
    """
    clock = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    request = AsyncMock(side_effect=[relation(), None, relation(), None])
    monkeypatch.setattr(client, "_request", request)

    async def run():
        """
        反复读取和过期读取应产生精确的中心请求次数。
        """
        first = await client.async_query(query())
        first.tmdb_id = 999
        assert (await client.async_query(query())).tmdb_id == 456
        assert await client.async_query(query(season=2)) is None
        assert await client.async_query(query(season=2)) is None
        assert request.await_count == 2
        clock[0] += transport.POSITIVE_TTL + 1
        assert (await client.async_query(query())).tmdb_id == 456
        monkeypatch.setattr(client, "_url", lambda: "https://other.example/recognize/mapping")
        assert await client.async_query(query()) is None
        assert request.await_count == 4
    asyncio.run(run())


@pytest.mark.parametrize("response", [None, {"code": 1}, {"code": 0, "data": {"item": {"tmdb_id": 0}}}])
def test_bad_response_enters_global_cooldown(client, monkeypatch, response):
    """
    非法或不可用响应只触发一次请求，后续不同身份在冷却中快速返回。
    """
    get = AsyncMock(return_value=None if response is None else Mock(status_code=200, json=Mock(return_value=response)))
    monkeypatch.setattr(transport, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_res=get)))

    async def run():
        """
        不同媒体连续失败也不会持续请求故障中心。
        """
        assert await client.async_query(query()) is None
        assert await client.async_query(query(season=2)) is None
        assert get.await_count == 1
    asyncio.run(run())


def test_request_total_timeout_and_concurrent_bound(client, monkeypatch):
    """
    故障总超时覆盖传输重试，重复在途查询不重复请求。
    """
    async def blocked(*_args, **_kwargs):
        """
        模拟不会主动返回的中心请求，依靠总预算中断。
        """
        await asyncio.Event().wait()
    get = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(transport, "QUERY_TIMEOUT", 0.03)
    monkeypatch.setattr(transport, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_res=get)))

    async def run():
        """
        两个相同查询只发一次请求且均在短预算内结束。
        """
        started = time.monotonic()
        assert await asyncio.gather(client.async_query(query()), client.async_query(query())) == [None, None]
        assert time.monotonic() - started < 0.3
        assert get.await_count == 1
        assert not client._inflight
    asyncio.run(run())


def test_query_validates_response_identity_and_disabled_service(client, monkeypatch):
    """
    即使中心响应格式有效，也不能使用其他来源或原生 ID 的关系。
    """
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=relation(media_id="other")))
    assert client.query(query()) is None
    monkeypatch.setattr(client, "_url", lambda: None)
    assert client.query(query()) is None
    client.share(relation())


def test_share_runs_in_owned_background_and_honors_revocation(client, monkeypatch):
    """
    共享立即写入本地缓存，后台根据中心撤销结果清除该命中。
    """
    complete = asyncio.Event()

    async def request(*_args, **_kwargs):
        """
        中心确认撤销时返回空关系，不允许长期保留本地错误目标。
        """
        complete.set()
        return None
    monkeypatch.setattr(client, "_request", request)

    async def run():
        """
        使用真实宿主循环登记，等待已归属后台任务完成。
        """
        owner = main_loop_registry.register(asyncio.get_running_loop())
        try:
            client.share(relation())
            assert (await client.async_query(query())).tmdb_id == 456
            await asyncio.wait_for(complete.wait(), 0.5)
            assert await client.async_query(query()) is None
            assert client.query(query()) is None
        finally:
            main_loop_registry.release(owner)
    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_conversion_uses_shared_identity_before_source_or_name_matching(asynchronous):
    """
    同异步转换均直接按共享 TMDB ID 取详情，并保留共享季号。
    """
    port = SimpleNamespace(query=Mock(return_value=relation()), async_query=AsyncMock(return_value=relation()), share=Mock())
    configure_media_mapping_port(port)
    owner = SimpleNamespace(tmdb_info=Mock(return_value={"id": 456, "media_type": "tv"}),
                            async_tmdb_info=AsyncMock(return_value={"id": 456, "media_type": "tv"}))
    args = dict(target_source=MediaSource.TMDB, media_source=MediaSource.AniList, media_id="123", mtype=MediaType.TV)
    result = asyncio.run(MediaChain.async_convert_media_identity(owner, **args)) if asynchronous else MediaChain.convert_media_identity(owner, **args)
    assert result == {"id": 456, "media_type": "tv", "season": 0}
    port.share.assert_not_called()


def test_miss_keeps_existing_conversion_and_shares_success(monkeypatch):
    """
    缺少关系时仍执行原匹配，成功关系以原来源身份上报。
    """
    from app.chain.media import projection
    port = SimpleNamespace(query=Mock(return_value=None), share=Mock())
    configure_media_mapping_port(port)
    monkeypatch.setattr(projection, "_convert_builtin_projection", Mock(return_value={"id": 456, "media_type": "tv", "season": 2}))
    result = MediaChain.convert_media_identity(SimpleNamespace(), target_source=MediaSource.TMDB,
                                              media_source=MediaSource.AniList, media_id="123", mtype=MediaType.TV, season=1)
    assert result["season"] == 2
    assert port.share.call_args.args[0] == relation(season=1, tmdb_season=2)


def test_reverse_conversion_never_queries_or_shares(monkeypatch):
    """
    原有 TMDB 到其他来源转换保持原行为，不接入关系共享。
    """
    from app.chain.media import projection
    port = SimpleNamespace(query=Mock(), share=Mock())
    configure_media_mapping_port(port)
    monkeypatch.setattr(projection, "_convert_builtin_projection", Mock(return_value={"id": "db-1"}))
    MediaChain.convert_media_identity(SimpleNamespace(), target_source=MediaSource.Douban,
                                     media_source=MediaSource.TMDB, media_id="456", mtype=MediaType.TV)
    port.query.assert_not_called()
    port.share.assert_not_called()


def test_browsing_endpoint_only_reads_existing_mapping():
    """
    详情辅助入口只读已有关系，不触发转换或共享。
    """
    port = SimpleNamespace(async_query=AsyncMock(return_value=relation()), share=Mock())
    configure_media_mapping_port(port)
    assert asyncio.run(endpoint.media_mapping(query())) == relation()
    port.share.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unavailable_shared_tmdb_detail_falls_back_to_original_conversion(monkeypatch, asynchronous):
    """
    共享目标无法读取时仍执行原转换，并共享本次有效匹配，避免旧关系阻断订阅。
    """
    from app.chain.media import projection

    port = SimpleNamespace(query=Mock(return_value=relation()), async_query=AsyncMock(return_value=relation()), share=Mock())
    configure_media_mapping_port(port)
    fresh = {"id": 789, "media_type": "tv", "season": 2}
    match = AsyncMock(return_value=fresh) if asynchronous else Mock(return_value=fresh)
    monkeypatch.setattr(projection, "_async_convert_builtin_projection" if asynchronous else "_convert_builtin_projection", match)
    owner = SimpleNamespace(tmdb_info=Mock(return_value=None), async_tmdb_info=AsyncMock(return_value=None))
    args = dict(target_source=MediaSource.TMDB, media_source=MediaSource.AniList, media_id="123", mtype=MediaType.TV)
    result = asyncio.run(MediaChain.async_convert_media_identity(owner, **args)) if asynchronous else MediaChain.convert_media_identity(owner, **args)
    assert result == fresh
    assert port.share.call_args.args[0] == relation(tmdb_id=789, tmdb_season=2)


@pytest.mark.parametrize("result", [{"id": 0, "media_type": "tv"}, {"id": 789, "media_type": "music"}, {"id": 789}])
def test_incomplete_plugin_result_is_returned_without_polluting_shared_relations(monkeypatch, result):
    """
    旧插件不提供完整 TMDB 类型或有效 ID 时保持兼容结果，但禁止写入公共关系。
    """
    from app.chain.media import projection

    port = SimpleNamespace(query=Mock(return_value=None), share=Mock())
    configure_media_mapping_port(port)
    monkeypatch.setattr(projection, "_convert_builtin_projection", Mock(return_value=result))
    actual = MediaChain.convert_media_identity(SimpleNamespace(), target_source=MediaSource.TMDB,
                                               media_source=MediaSource.AniList, media_id="123")
    assert actual == result
    port.share.assert_not_called()
