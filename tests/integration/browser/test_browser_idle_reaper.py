"""
验证浏览器主动空闲回收、线程归属及与排队操作和生命周期取消的竞态。
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.adapters.network import browser
from app.adapters.network.browser import BrowserSessionHelper
from app.runtime.tasks import TaskRegistry


@pytest.fixture
def sessions(monkeypatch):
    """
    隔离类级登记表并使用虚拟单调时钟，所有浏览器对象均为替身。
    """
    for name in (
        "_sessions", "_session_executors", "_session_executor_owners",
        "_session_thread_ids", "_pending_operations", "_idle_closures",
        "_idle_close_owners", "_explicit_closures",
    ):
        monkeypatch.setattr(BrowserSessionHelper, name, {})
    monkeypatch.setattr(BrowserSessionHelper, "_reaper_task", None)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(browser, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(BrowserSessionHelper, "REAP_INTERVAL_SECONDS", 0.001)
    contexts = []

    def launch(**_kwargs):
        """
        创建能记录关闭线程的浏览器上下文替身。
        """
        context = MagicMock()
        context.close_threads = []
        context.page = MagicMock()
        context.new_page.return_value = context.page
        context.close.side_effect = lambda: context.close_threads.append(threading.get_ident())
        contexts.append(context)
        return context

    monkeypatch.setattr(BrowserSessionHelper, "_launch_context", staticmethod(launch))
    yield SimpleNamespace(clock=clock, contexts=contexts, helper=BrowserSessionHelper())
    BrowserSessionHelper.close_all_sessions()
    assert not BrowserSessionHelper._pending_operations
    assert not BrowserSessionHelper._idle_closures
    assert not BrowserSessionHelper._idle_close_owners
    assert not BrowserSessionHelper._explicit_closures


def _create(sessions, key="idle", owner=None):
    """
    用真实会话执行器创建替身上下文并返回会话和其线程身份。
    """
    return sessions.helper.with_session(
        key, lambda session: (session, threading.get_ident()), owner=owner,
    )


async def _until(predicate):
    """
    只等待被测行为达到明确状态，超时失败而非依赖固定休眠猜测完成时间。
    """
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def _drain_checks():
    """
    等待本轮已投递的空闲检查完成，不触发新浏览器操作。
    """
    with BrowserSessionHelper._sessions_lock:
        futures = tuple(BrowserSessionHelper._idle_closures.values())
    for future in futures:
        future.result(timeout=3)


@pytest.mark.asyncio
async def test_reaper_expires_without_another_browser_call(sessions):
    """
    没有后续请求仍在原线程关闭页面和上下文，并释放会话和执行器登记。
    """
    session, thread_id = _create(sessions)
    page = session.active_page
    registry = TaskRegistry()
    BrowserSessionHelper.start_idle_reaper(registry)
    first_task = BrowserSessionHelper._reaper_task
    BrowserSessionHelper.start_idle_reaper(registry)
    assert BrowserSessionHelper._reaper_task is first_task
    assert [record.owner for record in registry.records] == ["browser.sessions.idle_reaper"]
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    try:
        await _until(lambda: bool(session.context.close_threads))
        assert session.context.close_threads == [thread_id]
        page.close.assert_called_once()
        assert not BrowserSessionHelper._sessions
        assert not BrowserSessionHelper._session_executors
        assert not BrowserSessionHelper._session_thread_ids
    finally:
        assert await registry.shutdown(timeout_seconds=3)
    assert first_task.cancelled()


@pytest.mark.asyncio
async def test_reaper_is_lazy_and_preserves_unexpired_session_on_shutdown(sessions):
    """
    启动扫描不物化浏览器；登记器关闭不提前释放仍属于消费者的活动会话。
    """
    registry = TaskRegistry()
    BrowserSessionHelper.start_idle_reaper(registry)
    assert not sessions.contexts
    assert not BrowserSessionHelper._session_executors
    session, _ = _create(sessions)
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS
    BrowserSessionHelper._prune_sessions()
    assert not BrowserSessionHelper._idle_closures
    assert await registry.shutdown(timeout_seconds=3)
    session.context.close.assert_not_called()
    assert BrowserSessionHelper._sessions["idle"] is session


@pytest.mark.parametrize("fails", [False, True])
def test_long_running_operation_is_not_idle_and_completion_renews_ttl(sessions, fails):
    """
    操作执行超过 TTL 不被误关，成功和失败均从真实完成时重新计算空闲期。
    """
    entered = threading.Event()
    release = threading.Event()

    def operation(session):
        entered.set()
        assert release.wait(3)
        if fails:
            raise ValueError("模拟操作失败")
        return session

    with ThreadPoolExecutor(max_workers=1) as callers:
        future = callers.submit(sessions.helper.with_session, "busy", operation)
        try:
            assert entered.wait(3)
            sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
            BrowserSessionHelper._prune_sessions()
            assert not BrowserSessionHelper._idle_closures
            sessions.contexts[0].close.assert_not_called()
        finally:
            release.set()
        if fails:
            with pytest.raises(ValueError, match="模拟操作失败"):
                future.result(timeout=3)
        else:
            future.result(timeout=3)
    session = BrowserSessionHelper._sessions["busy"]
    assert session.last_used_at == sessions.clock.now
    BrowserSessionHelper._prune_sessions()
    assert not BrowserSessionHelper._idle_closures
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    BrowserSessionHelper._prune_sessions()
    _drain_checks()
    session.context.close.assert_called_once()


def test_queued_operation_prevents_pending_idle_close(sessions, monkeypatch):
    """
    候选检查已排队但尚未执行时，新接受的操作使旧检查放弃关闭。
    """
    session, _ = _create(sessions)
    executor = BrowserSessionHelper._session_executors["idle"]
    blocker_entered = threading.Event()
    release = threading.Event()
    admitted = threading.Event()
    original_submit = executor.submit

    def blocker():
        blocker_entered.set()
        assert release.wait(3)

    original_submit(blocker)
    assert blocker_entered.wait(3)
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    BrowserSessionHelper._prune_sessions()
    closure = BrowserSessionHelper._idle_closures[executor]
    BrowserSessionHelper._prune_sessions()
    assert len(BrowserSessionHelper._idle_closures) == 1

    def submit_operation(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        admitted.set()
        return future

    monkeypatch.setattr(executor, "submit", submit_operation)
    with ThreadPoolExecutor(max_workers=1) as callers:
        operation = callers.submit(sessions.helper.with_session, "idle", lambda state: state)
        try:
            assert admitted.wait(3)
            assert BrowserSessionHelper._pending_operations[executor] == 1
        finally:
            release.set()
        assert operation.result(timeout=3) is session
        assert closure.result(timeout=3) is False
    session.context.close.assert_not_called()
    assert session.last_used_at == sessions.clock.now


@pytest.mark.parametrize("change", ["refresh", "session", "executor", "owner"])
def test_idle_candidate_is_revalidated_on_its_worker(sessions, change):
    """
    扫描快照中的时间、会话、执行器或 owner 已变化时，旧检查不能关闭资源。
    """
    session, _ = _create(sessions)
    executor = BrowserSessionHelper._session_executors["idle"]
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    expected_session = session
    expected_executor = executor
    expected_owner = session.owner
    if change == "refresh":
        session.last_used_at = sessions.clock.now
    elif change == "session":
        expected_session = object()
    elif change == "executor":
        expected_executor = object()
    else:
        expected_owner = object()
    assert executor.submit(
        BrowserSessionHelper._close_idle_session,
        "idle", expected_session, expected_executor, expected_owner,
    ).result(timeout=3) is False
    session.context.close.assert_not_called()


@pytest.mark.asyncio
async def test_slow_close_does_not_block_other_sessions_and_shutdown_retains_owner(sessions):
    """
    一个关闭阻塞不妨碍其他会话回收；停机超时保留任务直到关闭真实结束。
    """
    session, _ = _create(sessions, "slow")
    other, _ = _create(sessions, "other")
    entered = threading.Event()
    release = threading.Event()

    def slow_close():
        entered.set()
        assert release.wait(5)

    session.context.close.side_effect = slow_close
    registry = TaskRegistry()
    BrowserSessionHelper.start_idle_reaper(registry)
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    try:
        await _until(entered.is_set)
        await _until(lambda: bool(other.context.close_threads))
        assert await registry.shutdown(timeout_seconds=0.01) is False
        assert registry.records
        assert BrowserSessionHelper._idle_closures
        assert not BrowserSessionHelper._reaper_task.done()
    finally:
        release.set()
        assert await registry.shutdown(timeout_seconds=3)
    assert not BrowserSessionHelper._idle_closures


def test_old_idle_close_cannot_remove_replacement_with_same_key(sessions):
    """
    已撤销准入的旧上下文关闭期间，同名新会话可使用独立执行器且不会被旧关闭删除。
    """
    old, old_thread = _create(sessions)
    entered = threading.Event()
    release = threading.Event()

    def slow_close():
        assert threading.get_ident() == old_thread
        entered.set()
        assert release.wait(3)

    old.context.close.side_effect = slow_close
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    BrowserSessionHelper._prune_sessions()
    try:
        assert entered.wait(3)
        new, new_thread = _create(sessions)
        assert new is not old
        assert new_thread != old_thread
        new_executor = BrowserSessionHelper._session_executors["idle"]
    finally:
        release.set()
        _drain_checks()
    assert BrowserSessionHelper._sessions["idle"] is new
    assert BrowserSessionHelper._session_executors["idle"] is new_executor
    assert BrowserSessionHelper._session_thread_ids["idle"] == new_thread
    new.context.close.assert_not_called()


def test_owner_close_waits_for_already_retiring_session(sessions):
    """
    空闲回收已移除索引但仍在关闭时，所属作用域不能提前宣告资源释放完成。
    """
    owner = object()
    session, _ = _create(sessions, owner=owner)
    entered = threading.Event()
    release = threading.Event()

    def slow_close():
        entered.set()
        assert release.wait(3)

    session.context.close.side_effect = slow_close
    sessions.clock.now += BrowserSessionHelper.SESSION_TTL_SECONDS + 1
    BrowserSessionHelper._prune_sessions()
    with ThreadPoolExecutor(max_workers=1) as callers:
        try:
            assert entered.wait(3)
            closing = callers.submit(BrowserSessionHelper.close_owner, owner)
            assert not closing.done()
        finally:
            release.set()
        assert closing.result(timeout=3)


@pytest.mark.parametrize("close_all", [False, True], ids=["owner", "all"])
def test_shutdown_waits_for_explicit_close_after_index_removal(sessions, monkeypatch, close_all):
    """
    显式关闭已撤销会话准入但 I/O 未完成时，全局或 owner 收口仍等待真实终态。
    """
    owner = object()
    session, _ = _create(sessions, owner=owner)
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()

    def slow_close():
        entered.set()
        assert release.wait(3)

    session.context.close.side_effect = slow_close
    with ThreadPoolExecutor(max_workers=2) as callers:
        explicit = callers.submit(BrowserSessionHelper.close_session, "idle", owner)
        try:
            assert entered.wait(3)
            assert "idle" not in BrowserSessionHelper._sessions
            assert "idle" not in BrowserSessionHelper._session_executors
            completion = next(iter(BrowserSessionHelper._explicit_closures.values()))[1]
            original_result = completion.result

            def observe_wait(*args, **kwargs):
                waiting.set()
                return original_result(*args, **kwargs)

            monkeypatch.setattr(completion, "result", observe_wait)
            shutdown = callers.submit(
                BrowserSessionHelper.close_all_sessions if close_all
                else lambda: BrowserSessionHelper.close_owner(owner)
            )
            assert waiting.wait(3)
            assert not shutdown.done()
        finally:
            release.set()
        assert explicit.result(timeout=3)
        result = shutdown.result(timeout=3)
        assert result is None if close_all else result is True
    assert not BrowserSessionHelper._explicit_closures


def test_rejected_submission_does_not_leak_pending_operations(sessions, monkeypatch):
    """
    已关闭执行器拒绝提交时释放计数，并允许重试在新执行器上创建会话。
    """
    session, _ = _create(sessions)
    executor = BrowserSessionHelper._session_executors["idle"]
    executor.shutdown(wait=True)
    monkeypatch.setattr(BrowserSessionHelper, "_sessions", {})
    replacement, _ = _create(sessions)
    assert replacement is not session
    assert not BrowserSessionHelper._pending_operations
    session.context.close()
