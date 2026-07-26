import asyncio
from types import SimpleNamespace

import app.api.endpoints.search as search_endpoint


def test_large_replace_event_is_split_into_ordered_batches(monkeypatch):
    """超大最终结果应拆成首个 replace 和后续 append 批次。"""
    monkeypatch.setattr(search_endpoint, "_SSE_REPLACE_MAX_ITEMS", 2)
    source_event = {
        "type": "replace",
        "stage": "filtered",
        "items": [1, 2, 3, 4, 5],
        "total_items": 5,
    }

    async def _collect_events():
        """通过完整批处理适配器收集拆分后的最终结果。"""

        async def _source():
            """输出一个超大最终替换事件。"""
            yield source_event

        return [
            event
            async for event in search_endpoint._iter_batched_search_events(_source())
        ]

    events = asyncio.run(_collect_events())

    assert [event["type"] for event in events] == ["replace", "append", "append"]
    assert [event["batch_index"] for event in events] == [0, 1, 2]
    assert all(event["batch_count"] == 3 for event in events)
    assert all(event["replace_batch"] for event in events)
    assert [item for event in events for item in event["items"]] == source_event["items"]


def test_small_replace_event_keeps_original_protocol(monkeypatch):
    """小型最终结果应保持单个 replace 事件，兼容现有客户端。"""
    monkeypatch.setattr(search_endpoint, "_SSE_REPLACE_MAX_ITEMS", 2)
    source_event = {
        "type": "replace",
        "items": [1, 2],
        "total_items": 2,
    }

    assert list(search_endpoint._iter_replace_event_batches(source_event)) == [source_event]


def test_batched_search_events_emit_heartbeat_while_source_is_idle(monkeypatch):
    """上游长时间无业务事件时应持续输出心跳，避免连接被空闲超时关闭。"""
    monkeypatch.setattr(search_endpoint, "_SSE_HEARTBEAT_INTERVAL", 0.01)

    async def _read_heartbeat():
        """读取首个心跳并关闭仍在等待的上游迭代器。"""
        blocker = asyncio.Event()

        async def _source():
            """模拟长时间处于过滤匹配阶段的事件源。"""
            await blocker.wait()
            yield {"type": "done"}

        events = search_endpoint._iter_batched_search_events(_source())
        try:
            return await asyncio.wait_for(anext(events), timeout=0.5)
        finally:
            await events.aclose()

    assert asyncio.run(_read_heartbeat()) == {"type": "heartbeat"}


def test_search_stream_response_disables_proxy_buffering(monkeypatch):
    """搜索 SSE 响应应显式禁用缓存和 Nginx 代理缓冲。"""

    class FakeSearchChain:
        """提供无需外部依赖的空搜索流。"""

        def async_search_by_title_stream(self, **_kwargs):
            """返回立即完成的搜索流。"""

            async def _source():
                """输出一个完成事件。"""
                yield {"type": "done", "stage": "done", "total_items": 0}

            return _source()

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    async def _never_disconnected():
        """模拟始终在线的 SSE 客户端。"""
        return False

    request = SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/search/title/stream"),
        is_disconnected=_never_disconnected,
    )

    response = asyncio.run(
        search_endpoint.search_by_title_stream(request=request, keyword="Demo", _=None)
    )

    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
