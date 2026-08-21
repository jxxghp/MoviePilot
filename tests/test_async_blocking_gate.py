"""async 阻塞调用 ratchet 与 debug 模式测试。"""

import asyncio

import pytest

from scripts.architecture.async_blocking import compare_async_blocking


def test_async_blocking_ratchet_allows_removal_and_rejects_growth() -> None:
    """存量减少合法，新增或增加直接阻塞调用必须失败。"""
    baseline = {
        "app/api/a.py:stream:Path.read_text": 2,
        "app/agent/a.py:run:time.sleep": 1,
    }
    reduced = {"app/api/a.py:stream:Path.read_text": 1}
    increased = {
        "app/api/a.py:stream:Path.read_text": 3,
        "app/application/a.py:load:requests.get": 1,
    }

    assert compare_async_blocking(baseline, reduced) == []
    problems = compare_async_blocking(baseline, increased)
    assert any("增长" in problem for problem in problems)
    assert any("新增" in problem for problem in problems)


@pytest.mark.asyncio
async def test_asyncio_debug_is_enabled_for_async_tests() -> None:
    """专项异步测试必须启用慢 callback 和阻塞诊断所需的 debug 模式。"""
    assert asyncio.get_running_loop().get_debug() is True
