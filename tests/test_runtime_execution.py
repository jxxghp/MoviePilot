"""运行时同步 worker 的取消与容量合同回归。"""

import asyncio
import ast
import threading
from pathlib import Path

import pytest
from anyio.to_thread import current_default_thread_limiter

from app.adapters.external import market as market_adapter
from app.adapters.system.plugin import package as plugin_package_adapter
from app.runtime.execution import (
    await_task_to_terminal,
    run_in_threadpool_to_completion,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_host_uses_canonical_threadpool_boundary() -> None:
    """canonical 宿主不得重新直连框架线程池 helper。"""
    violations: list[str] = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if relative_path.startswith(
            ("app/plugins/", "app/runtime/compat/", "app/sdk/", "app/testing/")
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "fastapi.concurrency",
                "starlette.concurrency",
            }:
                continue
            if any(alias.name == "run_in_threadpool" for alias in node.names):
                violations.append(f"{relative_path}:{node.lineno}")

    assert violations == []


def test_plugin_file_adapters_share_runtime_completion_contract() -> None:
    """市场与插件包适配器不得各自维护另一套线程取消实现。"""
    assert market_adapter._await_thread_operation is run_in_threadpool_to_completion
    assert (
        plugin_package_adapter._await_thread_operation
        is run_in_threadpool_to_completion
    )


@pytest.mark.asyncio
async def test_await_task_to_terminal_ignores_repeated_cancellation() -> None:
    """调用方连续取消时，受保护任务仍须结束并返回真实结果。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def protected_operation() -> str:
        """阻塞到测试释放，用于观察受保护任务的真实终态。"""
        started.set()
        await release.wait()
        return "completed"

    protected_task = asyncio.create_task(protected_operation())
    waiter = asyncio.create_task(await_task_to_terminal(protected_task))
    await started.wait()

    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    assert waiter.done() is False

    release.set()
    assert await waiter == "completed"


@pytest.mark.asyncio
async def test_threadpool_capacity_is_held_until_cancelled_call_finishes() -> None:
    """调用方取消后，执行令牌必须由真实同步调用持有到终态。"""
    limiter = current_default_thread_limiter()
    original_capacity = limiter.total_tokens
    release = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def blocking_call(started: threading.Event) -> None:
        started.set()
        release.wait()

    limiter.total_tokens = 1
    first = asyncio.create_task(
        run_in_threadpool_to_completion(blocking_call, first_started)
    )
    second = None
    try:
        while not first_started.is_set():
            await asyncio.sleep(0)

        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)

        assert first.done() is False
        assert limiter.borrowed_tokens == 1

        second = asyncio.create_task(
            run_in_threadpool_to_completion(blocking_call, second_started)
        )
        await asyncio.sleep(0.01)
        assert second_started.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    finally:
        release.set()
        if not first.done():
            await asyncio.gather(first, return_exceptions=True)
        if second is not None and not second.done():
            await asyncio.gather(second, return_exceptions=True)
        limiter.total_tokens = original_capacity


@pytest.mark.asyncio
async def test_cancelled_threadpool_call_preserves_worker_failure_as_cause() -> None:
    """调用方取消优先返回，线程终态异常仍保留为诊断原因。"""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[dict] = []
    release = threading.Event()
    started = threading.Event()

    def failing_call() -> None:
        started.set()
        release.wait()
        raise ValueError("worker failed")

    task = asyncio.create_task(run_in_threadpool_to_completion(failing_call))
    while not started.is_set():
        await asyncio.sleep(0)

    task.cancel()
    release.set()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        with pytest.raises(asyncio.CancelledError) as error_info:
            await task
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)
    assert isinstance(error_info.value.__cause__, ValueError)
    assert loop_errors == []
