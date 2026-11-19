"""
详情页按需查找 TMDB 关系的协调、转换复用和接口回归。
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.external import mapping as transport
from app.api.endpoints import media as endpoint
from app.application.server import lookup as lookup_module
from app.application.server.lookup import (
    MediaMappingLookup,
    MediaMappingLookupBusy,
    MediaMappingLookupFailed,
    MediaMappingLookupUnavailable,
)
from app.application.server.mapping import configure_media_mapping_port
from app.chain.media import MediaChain
from app.runtime.tasks import TaskRegistry
from app.schemas.mapping import MediaMapping, MediaMappingMatchQuery, MediaMappingQuery
from app.schemas.types import MediaSource, MediaType


def match_query(**overrides) -> MediaMappingMatchQuery:
    """
    构造豆瓣剧集第 2 季的查找请求，可按需覆盖来源、ID、类型和季号。
    """
    values = {"media_source": MediaSource.Douban, "media_id": "1001", "type": "tv", "season": 2, **overrides}
    return MediaMappingMatchQuery(**values)


def found(query: MediaMappingQuery, **overrides) -> MediaMapping:
    """
    为查询构造 TMDB 剧集关系，默认季号与原来源相同。
    """
    values = {"tmdb_id": 456, "tmdb_type": "tv", "tmdb_season": query.season, **overrides}
    if query.type == "movie":
        values = {"tmdb_id": 456, "tmdb_type": "movie", "tmdb_season": None, **overrides}
    return MediaMapping(**query.model_dump(), **values)


def blocking_matcher(gate: asyncio.Event) -> AsyncMock:
    """
    返回等待闸门打开后才给出结论的匹配替身，用于观察在途状态。
    """
    async def match(query: MediaMappingMatchQuery) -> MediaMapping:
        """
        模拟耗时的订阅同款匹配。
        """
        await gate.wait()
        return found(query)
    return AsyncMock(side_effect=match)


def test_shared_relation_takes_priority_over_local_record() -> None:
    """
    共享关系始终优先：本地记录没找到或找到旧目标后，客户端出现的新关系立即生效。
    """
    missing, other = match_query(), match_query(media_id="2002")
    corrected = found(other, tmdb_id=999)
    port = SimpleNamespace(async_query=AsyncMock(side_effect=[None, None, found(missing), None, corrected]))
    matcher = AsyncMock(side_effect=[None, found(other)])

    async def run() -> None:
        """
        依次覆盖没找到后被订阅补齐、本地匹配后被 Server 修正两种情况。
        """
        lookup = MediaMappingLookup(matcher, port=lambda: port, tasks=TaskRegistry)
        assert await lookup.lookup(missing) is None
        assert await lookup.lookup(missing) is None
        assert await lookup.lookup(missing) == found(missing)
        assert await lookup.lookup(other) == found(other)
        assert await lookup.lookup(other) == corrected
        assert matcher.await_count == 2
    asyncio.run(run())


def test_local_record_covers_disabled_sharing_and_expires() -> None:
    """
    共享未装配时本地记录同时记住没找到和找到，过期后重新匹配，读取结果互不影响。
    """
    clock = [100.0]
    query = match_query()
    matcher = AsyncMock(side_effect=[None, found(query)])

    async def run() -> None:
        """
        有效期内不重复匹配，过期后得到新结论并继续复用。
        """
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=TaskRegistry, clock=lambda: clock[0])
        assert await lookup.lookup(query) is None
        assert await lookup.lookup(query) is None
        clock[0] += lookup_module.RECORD_TTL + 1
        first = await lookup.lookup(query)
        first.tmdb_id = 1
        assert await lookup.lookup(query) == found(query)
        assert matcher.await_count == 2
    asyncio.run(run())


def test_record_limit_evicts_least_recently_used(monkeypatch) -> None:
    """
    条数超过上限时淘汰最久未访问的记录，刚访问过的记录保留。
    """
    monkeypatch.setattr(lookup_module, "RECORD_LIMIT", 2)
    first, second, third = (match_query(media_id=str(value)) for value in (1, 2, 3))
    matcher = AsyncMock(side_effect=lambda query: found(query))

    async def run() -> None:
        """
        访问顺序决定淘汰对象。
        """
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=TaskRegistry)
        for query in (first, second, first, third):
            await lookup.lookup(query)
        assert matcher.await_count == 3
        await lookup.lookup(first)
        assert matcher.await_count == 3
        await lookup.lookup(second)
        assert matcher.await_count == 4
    asyncio.run(run())


def test_concurrent_requests_share_one_match() -> None:
    """
    同一条目的并发请求只触发一次匹配，并得到相同结果。
    """
    async def run() -> None:
        """
        匹配未完成前的所有请求都合并到同一个后台任务。
        """
        gate = asyncio.Event()
        matcher = blocking_matcher(gate)
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=TaskRegistry)
        waiters = [asyncio.create_task(lookup.lookup(match_query())) for _ in range(3)]
        await asyncio.sleep(0)
        assert len(lookup._flights) == 1
        gate.set()
        assert await asyncio.gather(*waiters) == [found(match_query())] * 3
        assert matcher.await_count == 1
    asyncio.run(run())


def test_distinct_entries_beyond_limit_are_rejected() -> None:
    """
    同时查找的不同条目达到上限后拒绝新条目，名额释放后可以继续查找。
    """
    async def run() -> None:
        """
        第 5 个不同条目在前 4 个完成前被拒绝。
        """
        gate = asyncio.Event()
        lookup = MediaMappingLookup(blocking_matcher(gate), port=lambda: None, tasks=TaskRegistry)
        waiters = [
            asyncio.create_task(lookup.lookup(match_query(media_id=str(value))))
            for value in range(1, lookup_module.MATCH_LIMIT + 1)
        ]
        await asyncio.sleep(0)
        with pytest.raises(MediaMappingLookupBusy):
            await lookup.lookup(match_query(media_id="extra"))
        gate.set()
        await asyncio.gather(*waiters)
        assert await lookup.lookup(match_query(media_id="extra")) == found(match_query(media_id="extra"))
    asyncio.run(run())


def test_cancelled_waiter_keeps_match_running_and_slot_occupied() -> None:
    """
    请求断开不取消后台查找；查找期间继续占用名额，完成后写入记录。
    """
    async def run() -> None:
        """
        取消等待方后匹配仍然完成，再次请求直接复用结论。
        """
        registry = TaskRegistry()
        gate = asyncio.Event()
        matcher = blocking_matcher(gate)
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=lambda: registry)
        waiter = asyncio.create_task(lookup.lookup(match_query()))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert len(lookup._flights) == 1
        gate.set()
        await asyncio.gather(*(record.task for record in registry.records))
        assert not lookup._flights
        assert await lookup.lookup(match_query()) == found(match_query())
        assert matcher.await_count == 1
    asyncio.run(run())


def test_task_cancelled_before_start_cleans_up_without_record() -> None:
    """
    后台任务在首次调度前被关停取消时，协程体不会执行，仍须清理在途项且不写记录。
    """
    async def run() -> None:
        """
        关停取消尚未启动的查找，等待方得到不可用，之后拒绝创建新任务。
        """
        registry = TaskRegistry()
        matcher = AsyncMock(return_value=None)
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=lambda: registry)
        waiter = asyncio.create_task(lookup.lookup(match_query()))
        await asyncio.sleep(0)
        assert len(lookup._flights) == 1
        assert await registry.shutdown(timeout_seconds=1)
        with pytest.raises(MediaMappingLookupUnavailable):
            await waiter
        matcher.assert_not_awaited()
        assert not lookup._flights
        assert not lookup._records
        with pytest.raises(MediaMappingLookupUnavailable):
            await lookup.lookup(match_query())
        assert not lookup._flights
    asyncio.run(run())


def test_timeout_and_error_fail_without_record(monkeypatch) -> None:
    """
    后台超时和匹配异常都按失败处理，不写记录、释放名额，下次重新匹配。
    """
    calls = []

    async def match(query: MediaMappingMatchQuery) -> MediaMapping:
        """
        第一次永不返回，第二次抛出异常，第三次正常返回。
        """
        calls.append(query)
        if len(calls) == 1:
            await asyncio.Event().wait()
        if len(calls) == 2:
            raise RuntimeError("插件转换异常")
        return found(query)
    log = Mock()
    monkeypatch.setattr(lookup_module, "logger", log)
    monkeypatch.setattr(lookup_module, "MATCH_TIMEOUT", 0.02)

    async def run() -> None:
        """
        两次失败后第三次得到结论。
        """
        lookup = MediaMappingLookup(match, port=lambda: None, tasks=TaskRegistry)
        for _ in range(2):
            with pytest.raises(MediaMappingLookupFailed):
                await lookup.lookup(match_query())
            assert not lookup._flights
            assert not lookup._records
        assert await lookup.lookup(match_query()) == found(match_query())
    asyncio.run(run())
    assert "超时" in log.warning.call_args.args[0]
    assert "插件转换异常" in log.error.call_args.args[0]


def test_matcher_own_timeout_is_logged_as_failure_with_cause(monkeypatch) -> None:
    """
    匹配内部自行抛出的超时不是总预算到期，按失败记录原始原因，不冒充 180 秒超时。
    """
    log = Mock()
    monkeypatch.setattr(lookup_module, "logger", log)
    matcher = AsyncMock(side_effect=TimeoutError("上游转换超时"))

    async def run() -> None:
        """
        失败不写记录，日志保留原始异常。
        """
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=TaskRegistry)
        with pytest.raises(MediaMappingLookupFailed):
            await lookup.lookup(match_query())
        assert not lookup._records
    asyncio.run(run())
    log.warning.assert_not_called()
    assert "上游转换超时" in log.error.call_args.args[0]


def test_conclusion_logs_describe_entry(monkeypatch) -> None:
    """
    每次后台查找结束只打一行结论日志，命中本地记录时不再记录。
    """
    log = Mock()
    monkeypatch.setattr(lookup_module, "logger", log)
    movie = match_query(type="movie", season=None)
    matcher = AsyncMock(side_effect=[found(match_query()), None])

    async def run() -> None:
        """
        找到与没找到各一次，再次读取记录不追加日志。
        """
        lookup = MediaMappingLookup(matcher, port=lambda: None, tasks=TaskRegistry)
        await lookup.lookup(match_query())
        await lookup.lookup(movie)
        await lookup.lookup(movie)
    asyncio.run(run())
    assert [call.args[0] for call in log.info.call_args_list] == [
        "详情页查找 TMDB 链接：douban:1001（电视剧 第2季） → 找到 TMDB tv/456 第2季",
        "详情页查找 TMDB 链接：douban:1001（电影） → 未找到",
    ]


def test_lookup_reads_shared_client_cache_written_by_detail_query(monkeypatch) -> None:
    """
    查找请求与详情页 GET 使用同一共享客户端缓存键，命中时不匹配。
    """
    config = {"MP_SERVER_HOST": "https://center.example", "MEDIA_RECOGNIZE_SHARE": True, "PROXY": None}
    monkeypatch.setattr(transport, "get_runtime_setting", config.get)
    client = transport.MediaMappingClient()
    query = match_query()
    url = client._url()
    assert url is not None
    with client._lock:
        client._remember(client._key(url, MediaMappingQuery(**query.model_dump())), found(query), transport.POSITIVE_TTL)
    matcher = AsyncMock()
    lookup = MediaMappingLookup(matcher, port=lambda: client, tasks=TaskRegistry)
    assert asyncio.run(lookup.lookup(query)) == found(query)
    matcher.assert_not_awaited()


@pytest.mark.parametrize(
    ("query", "result"),
    [
        (match_query(type="movie", season=None), {"id": 456, "media_type": MediaType.MOVIE}),
        (match_query(), {"id": 456, "media_type": MediaType.TV, "season": 1}),
        (match_query(media_source=MediaSource.Bangumi, media_id="88", season=None),
         {"id": 456, "media_type": MediaType.TV, "season": 1}),
        (match_query(media_source=MediaSource.AniList, media_id="99", season=None),
         {"id": 456, "media_type": MediaType.TV, "season": 0}),
    ],
)
def test_chain_match_reuses_conversion_and_returns_shared_relation(monkeypatch, query, result) -> None:
    """
    Chain 方法复用订阅同款转换，返回值与本次上报的关系逐字段一致。
    """
    from app.chain.media import projection

    port = SimpleNamespace(async_query=AsyncMock(return_value=None), share=Mock())
    configure_media_mapping_port(port)
    convert = AsyncMock(return_value=result)
    monkeypatch.setattr(projection, "_async_convert_builtin_projection", convert)
    actual = asyncio.run(MediaChain.async_match_media_mapping(SimpleNamespace(), query))
    assert actual is not None
    assert actual == port.share.call_args.args[0]
    assert actual.model_dump(include={"media_source", "media_id", "type", "season"}) == query.model_dump()
    assert actual.tmdb_id == 456
    convert.assert_awaited_once()


def test_chain_match_supports_plugin_source_through_conversion_event() -> None:
    """
    插件来源与订阅一致，通过转换事件得到 TMDB 详情并写回共享表。
    """
    port = SimpleNamespace(async_query=AsyncMock(return_value=None), share=Mock())
    configure_media_mapping_port(port)

    async def send(_etype, data):
        """
        模拟插件填充 TMDB 详情。
        """
        data.media_dict = {"id": 789, "media_type": MediaType.MOVIE}
        return SimpleNamespace(event_data=data)
    owner = SimpleNamespace(eventmanager=SimpleNamespace(async_send_event=AsyncMock(side_effect=send)))
    query = match_query(media_source=MediaSource("examplesource"), media_id="abc", type="movie", season=None)
    actual = asyncio.run(MediaChain.async_match_media_mapping(owner, query))
    assert actual == found(query, tmdb_id=789)
    assert port.share.call_args.args[0] == actual


@pytest.mark.parametrize("result", [None, {"id": 0, "media_type": MediaType.TV}, {"id": 789, "media_type": "music"}])
def test_chain_match_returns_none_without_valid_identity(monkeypatch, result) -> None:
    """
    转换没有结果或结果缺少有效 TMDB 身份时返回 None，也不写回共享表。
    """
    from app.chain.media import projection

    port = SimpleNamespace(async_query=AsyncMock(return_value=None), share=Mock())
    configure_media_mapping_port(port)
    monkeypatch.setattr(projection, "_async_convert_builtin_projection", AsyncMock(return_value=result))
    assert asyncio.run(MediaChain.async_match_media_mapping(SimpleNamespace(), match_query())) is None
    port.share.assert_not_called()


def test_chain_match_rejects_unsupported_identity_without_conversion(monkeypatch) -> None:
    """
    来源 ID 不符合转换规则时直接返回 None，不进入转换，也不写回共享表。
    """
    from app.chain.media import projection

    port = SimpleNamespace(async_query=AsyncMock(return_value=None), share=Mock())
    configure_media_mapping_port(port)
    convert = AsyncMock()
    monkeypatch.setattr(projection, "_async_convert_builtin_projection", convert)
    query = match_query(media_source=MediaSource.Bangumi, media_id="not-a-number", season=None)
    assert asyncio.run(MediaChain.async_match_media_mapping(SimpleNamespace(), query)) is None
    convert.assert_not_awaited()
    port.async_query.assert_not_awaited()
    port.share.assert_not_called()


def test_chain_match_still_returns_relation_when_sharing_disabled(monkeypatch) -> None:
    """
    共享端口未装配时仍返回本次转换结果，只是不查询也不上报。
    """
    from app.chain.media import projection

    configure_media_mapping_port(None)
    monkeypatch.setattr(
        projection, "_async_convert_builtin_projection",
        AsyncMock(return_value={"id": 456, "media_type": MediaType.TV, "season": 2}),
    )
    assert asyncio.run(MediaChain.async_match_media_mapping(SimpleNamespace(), match_query())) == found(match_query())


@pytest.fixture
def match_api(monkeypatch) -> Iterator[SimpleNamespace]:
    """
    只挂载媒体路由并替换认证与协调对象，检验参数校验和错误翻译。

    :param monkeypatch: 用例结束后恢复协调对象读取的 pytest 夹具
    :yields: HTTP 客户端与协调对象替身
    """
    stub = SimpleNamespace(lookup=AsyncMock(return_value=found(match_query())))
    monkeypatch.setattr(endpoint, "get_media_mapping_lookup", lambda: stub)
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/v1/media")
    app.dependency_overrides[endpoint.get_current_active_user_async] = lambda: SimpleNamespace(is_active=True)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, stub=stub, app=app)


def test_match_endpoint_returns_unified_response(match_api) -> None:
    """
    正常结果使用统一响应格式，请求体按查找模型校验后传给协调对象。
    """
    body = {"media_source": "douban", "media_id": "1001", "type": "tv", "season": 2}
    response = match_api.client.post("/api/v1/media/mapping/match", json=body)
    assert response.status_code == 200
    assert response.json()["data"] == found(match_query()).model_dump(mode="json")
    assert match_api.stub.lookup.await_args.args[0] == match_query()


@pytest.mark.parametrize(
    "body",
    [
        {"media_source": "douban", "media_id": "1001"},
        {"media_source": "douban", "media_id": "1001", "type": "movie", "season": 1},
        {"media_source": "themoviedb", "media_id": "1001", "type": "tv"},
        {"media_source": "douban", "media_id": "1001", "type": "tv", "extra": 1},
    ],
)
def test_match_endpoint_rejects_invalid_body(match_api, body) -> None:
    """
    缺少类型、电影带季号、TMDB 来源或多余字段都在进入协调对象前被拒绝。
    """
    response = match_api.client.post("/api/v1/media/mapping/match", json=body)
    assert response.status_code == 422
    match_api.stub.lookup.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status"),
    [(MediaMappingLookupBusy(), 429), (MediaMappingLookupFailed(), 502), (MediaMappingLookupUnavailable(), 503)],
)
def test_match_endpoint_translates_lookup_errors(match_api, error, status) -> None:
    """
    协调对象的已满、失败和不可用分别翻译为 429、502 和 503。
    """
    match_api.stub.lookup.side_effect = error
    response = match_api.client.post("/api/v1/media/mapping/match",
                                     json={"media_source": "douban", "media_id": "1001", "type": "tv"})
    assert response.status_code == status


def test_match_endpoint_requires_assembled_lookup(match_api, monkeypatch) -> None:
    """
    协调对象未装配时返回 503。
    """
    monkeypatch.setattr(endpoint, "get_media_mapping_lookup", lambda: None)
    response = match_api.client.post("/api/v1/media/mapping/match",
                                     json={"media_source": "douban", "media_id": "1001", "type": "tv"})
    assert response.status_code == 503


def test_match_endpoint_requires_same_authentication_as_lookup_endpoint() -> None:
    """
    按需查找与只读查询使用同一登录用户依赖，未登录请求不会进入协调对象。
    """
    def dependencies(path: str, method: str) -> set:
        """
        读取指定路由声明的直接依赖。
        """
        route = next(item for item in endpoint.router.routes
                     if getattr(item, "path", "") == path and method in getattr(item, "methods", set()))
        return {dependency.call for dependency in route.dependant.dependencies}

    assert endpoint.get_current_active_user_async in dependencies("/mapping/match", "POST")
    assert endpoint.get_current_active_user_async in dependencies("/mapping", "GET")
