"""
推荐状态轮询不得重复读取与反序列化全量搜索结果。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints.search import recommend_search_results


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["idle", "running", "completed", "error"])
async def test_check_only_reads_status_without_search_cache_or_task_mutation(status):
    """
    所有只读状态均直接响应，包含空搜索缓存时的运行中状态和对外错误说明。
    """
    current = {"status": status}
    if status == "error":
        current["error"] = "推荐失败"
    chain = SimpleNamespace(
        is_ai_recommend_enabled=True,
        async_last_search_results=AsyncMock(side_effect=AssertionError("不应读取资源缓存")),
        get_current_recommend_status_only=Mock(return_value=current),
        cancel_ai_recommend=Mock(),
        start_recommend_task=Mock(),
    )
    with patch("app.api.endpoints.search.SearchChain", return_value=chain):
        result = await recommend_search_results(filtered_indices=None, check_only=True, force=False)
    assert result.success is (status != "error")
    assert result.data["status"] == status
    if status == "error":
        assert result.message == "推荐失败"
    chain.async_last_search_results.assert_not_awaited()
    chain.cancel_ai_recommend.assert_not_called()
    chain.start_recommend_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_disabled_recommend_skips_cache_even_for_force(force):
    """
    未开启推荐时立即返回禁用状态，强制参数也不能读取资源或创建任务。
    """
    chain = SimpleNamespace(is_ai_recommend_enabled=False, async_last_search_results=AsyncMock())
    with patch("app.api.endpoints.search.SearchChain", return_value=chain):
        result = await recommend_search_results(filtered_indices=None, check_only=False, force=force)
    assert result.data == {"status": "disabled"}
    chain.async_last_search_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_keeps_precedence_over_check_only_and_receives_original_results():
    """
    同时传入强制与只读参数时保留既有强制语义，并把完整候选仅读取一次后交给任务。
    """
    results = [object(), object()]
    chain = SimpleNamespace(
        is_ai_recommend_enabled=True,
        async_last_search_results=AsyncMock(return_value=results),
        cancel_ai_recommend=Mock(), start_recommend_task=Mock(),
    )
    with patch("app.api.endpoints.search.SearchChain", return_value=chain):
        result = await recommend_search_results(filtered_indices=[1], check_only=True, force=True)
    assert result.data == {"status": "running"}
    chain.async_last_search_results.assert_awaited_once()
    chain.cancel_ai_recommend.assert_called_once()
    chain.start_recommend_task.assert_called_once_with([1], 2, results)
