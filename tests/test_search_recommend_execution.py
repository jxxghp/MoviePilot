"""AI 搜索推荐执行与代际栅栏回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.application.configuration import ChainRuntimeConfig
from app.chain.search import SearchChain
from app.chain.search import recommend as recommend_module
from app.chain.search.recommend import recommend_coordinator
from app.runtime.tasks import TaskRegistry


def _result(title: str):
    """构造一条具有稳定推荐身份的候选资源。"""
    return SimpleNamespace(
        torrent_info=SimpleNamespace(
            site=1,
            torrent_id=title,
            title=title,
            description="",
            size=1024,
            seeders=1,
            category="电影",
        )
    )


def _chain() -> SearchChain:
    """构造只包含推荐执行所需依赖的搜索链。"""
    chain = object.__new__(SearchChain)
    chain.runtime_config = ChainRuntimeConfig(
        media_extensions=(),
        ai_agent_enable=True,
        ai_recommend_enabled=True,
        ai_recommend_max_items=50,
    )
    chain.remove_cache = lambda _filename: None
    chain.async_save_cache = AsyncMock()
    return chain


def test_request_hash_distinguishes_same_count_candidates():
    """候选数量相同但资源身份不同时不得复用同一推荐请求。"""
    first = SearchChain._calculate_recommend_request_hash(None, 1, [_result("资源一")])
    second = SearchChain._calculate_recommend_request_hash(None, 1, [_result("资源二")])

    assert first != second


def test_execution_helpers_are_not_exported_on_search_facade():
    """推荐执行步骤应留在 owner 内部，不扩大插件可见的 SearchChain 门面。"""
    assert "_generate_recommend_indices" not in SearchChain.__dict__
    assert "_run_recommend" not in SearchChain.__dict__
    assert "_parse_recommend_response" not in SearchChain.__dict__
    assert "_recommend_prompt" not in SearchChain.__dict__


def test_empty_candidates_publish_error_and_finish(monkeypatch):
    """没有有效候选时应发布错误并结束任务，不调用模型或写缓存。"""

    async def scenario():
        recommend_coordinator.cancel()
        registry = TaskRegistry()
        monkeypatch.setattr(recommend_module, "get_task_registry", lambda: registry)
        chain = _chain()
        chain._invoke_recommend_llm = AsyncMock(side_effect=AssertionError("空候选不应调用模型"))
        try:
            chain.start_recommend_task(
                filtered_indices=None,
                search_results_count=1,
                results=[SimpleNamespace(torrent_info=None)],
            )
            task = recommend_coordinator.snapshot().task
            assert task is not None
            await task

            state = recommend_coordinator.snapshot()
            assert state.error == "没有可用于AI推荐的资源"
            assert not state.running
            assert state.task is None
            chain._invoke_recommend_llm.assert_not_awaited()
            chain.async_save_cache.assert_not_awaited()
        finally:
            recommend_coordinator.cancel()

    asyncio.run(scenario())


def test_stale_generation_cannot_publish_or_cache(monkeypatch):
    """旧任务即使忽略取消并返回结果，也不得覆盖新代际状态和缓存。"""

    async def scenario():
        recommend_coordinator.cancel()
        registry = TaskRegistry()
        monkeypatch.setattr(recommend_module, "get_task_registry", lambda: registry)
        chain = _chain()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        saved = []
        call_count = 0

        async def invoke(_text: str) -> str:
            """让第一代任务跨越第二代完成点后再返回。"""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    await release_first.wait()
            return "[0]"

        async def save_cache(payload, filename):
            """记录真正越过代际栅栏的缓存写入。"""
            saved.append((filename, payload))

        chain._invoke_recommend_llm = invoke
        chain.async_save_cache = save_cache
        try:
            chain.start_recommend_task(None, 1, [_result("旧资源")])
            first_task = recommend_coordinator.snapshot().task
            assert first_task is not None
            await asyncio.wait_for(first_started.wait(), timeout=1)

            chain.start_recommend_task(None, 1, [_result("新资源")])
            second_state = recommend_coordinator.snapshot()
            second_task = second_state.task
            assert second_task is not None
            await second_task

            release_first.set()
            await first_task

            state = recommend_coordinator.snapshot()
            assert state.request_hash == second_state.request_hash
            assert state.result == [0]
            assert not state.running
            assert state.task is None
            assert saved == [
                (
                    "__ai_recommend_indices__",
                    {"request_hash": second_state.request_hash, "results": [0]},
                )
            ]
        finally:
            release_first.set()
            task = recommend_coordinator.cancel()
            if task and not task.done():
                task.cancel()
                await task

    asyncio.run(scenario())
