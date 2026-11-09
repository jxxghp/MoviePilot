"""
验证宿主、SDK 与旧插件共用实例名额，以及启动、关闭和取消的真实时序。
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.adapters.network import browser
from app.adapters.system.host import OwnedProcessTree
from app.agent.terminal.ownership import TerminalScope
from app.agent.tools.impl.browse_webpage import BrowseWebpageTool
from app.runtime.browser import BrowserCapacity, BrowserQueueCancelledError, browser_request
from app.startup.initializers import modules as modules_initializer
from app.startup.initializers import plugins as plugins_initializer


class _Browser:
    """
    模拟独立浏览器进程，关闭失败时继续保持连接和名额。
    """

    def __init__(self) -> None:
        """
        初始化连接事实、断开监听器和可控关闭失败次数。
        """
        self.closed = False
        self.close_failures = 0
        self.listeners = []
        self.close_threads = []

    def on(self, event, callback):
        """
        只登记被测实例需要的进程断开通知。
        """
        assert event == "disconnected"
        self.listeners.append(callback)

    def is_connected(self):
        """
        返回模拟进程是否仍然存活。
        """
        return not self.closed

    def disconnect(self):
        """
        模拟进程退出，向全部持有者发布真实终态。
        """
        self.closed = True
        for listener in self.listeners:
            listener()

    def close(self, **_kwargs):
        """
        仅成功关闭才发布断开事件，支持在原句柄上重试。
        """
        self.close_threads.append(threading.get_ident())
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("close failed")
        self.disconnect()


class _AsyncBrowser(_Browser):
    """
    支持暂停关闭过程的异步进程替身。
    """

    def __init__(self, provider):
        """
        复用同步进程状态，并读取测试设置的异步关闭屏障。
        """
        super().__init__()
        self.provider = provider

    async def close(self, **kwargs):
        """
        等到测试放行后才确认进程已退出。
        """
        if self.provider.close_started is not None:
            self.provider.close_started.set()
            await self.provider.finish_close.wait()
        super().close(**kwargs)


class _WatchTree:
    """
    可控的专属进程树替身，用于验证监控线程确认退出后才归还名额。
    """

    def __init__(self) -> None:
        """
        初始化未退出状态。
        """
        self.terminated = False

    @property
    def stopped(self) -> bool:
        """
        返回测试替身是否已经被强制回收。
        """
        return self.terminated

    def terminate(self, _timeout: float) -> bool:
        """
        记录强制回收并报告已经确认退出。
        """
        self.terminated = True
        return True


@pytest.fixture
def provider(monkeypatch):
    """
    用同一组六类 SDK 入口模拟嵌套启动，不下载内核或访问授权服务器。
    """
    capacity = BrowserCapacity()
    module = ModuleType("cloakbrowser")
    implementation = ModuleType("cloakbrowser.browser")
    state = SimpleNamespace(
        module=module, implementation=implementation, capacity=capacity, created=[],
        settings={"CLOAKBROWSER_MAX_CONCURRENCY": 1, "CLOAKBROWSER_QUEUE_TIMEOUT": 0.5},
        fail_launch=False, fail_context=False, fail_page=False,
        launch_started=None, finish_launch=None, close_started=None, finish_close=None,
    )

    def make_browser(asynchronous=False):
        """
        创建一份独立实例，并记录是否出现超出容量的真实存活进程。
        """
        if state.fail_launch:
            raise RuntimeError("launch failed")
        value = _AsyncBrowser(state) if asynchronous else _Browser()
        state.created.append(value)
        assert sum(not item.closed for item in state.created) <= state.settings["CLOAKBROWSER_MAX_CONCURRENCY"]
        return value

    def make_context(value):
        """
        保留 SDK 的上下文关闭会一并关闭浏览器的合同。
        """
        if state.fail_context:
            raise RuntimeError("context failed")
        context = MagicMock()
        context.browser = value
        context.close = value.close
        context.new_page.return_value.title.return_value = "Ready"
        if state.fail_page:
            context.new_page.side_effect = RuntimeError("page failed")
        return context

    def launch(*_args, **_kwargs):
        """
        模拟公开的同步浏览器启动。
        """
        return make_browser()

    def launch_context(*args, **kwargs):
        """
        通过同一子模块的 launch 嵌套启动，不能二次取得名额。
        """
        return make_context(implementation.launch(*args, **kwargs))

    def launch_persistent_context(*_args, **_kwargs):
        """
        模拟直接返回持久化上下文的入口。
        """
        return make_context(make_browser())

    async def launch_async(*_args, **_kwargs):
        """
        可在浏览器创建前暂停，重现调用方取消而供应方仍在启动的情况。
        """
        if state.launch_started is not None:
            state.launch_started.set()
            await state.finish_launch.wait()
        return make_browser(asynchronous=True)

    async def launch_context_async(*args, **kwargs):
        """
        异步上下文内部也经同一组已包装的浏览器入口启动。
        """
        return make_context(await implementation.launch_async(*args, **kwargs))

    async def launch_persistent_context_async(*_args, **_kwargs):
        """
        模拟异步持久化上下文的直接启动入口。
        """
        return make_context(make_browser(asynchronous=True))

    for entrypoint in (
        launch, launch_context, launch_persistent_context,
        launch_async, launch_context_async, launch_persistent_context_async,
    ):
        setattr(module, entrypoint.__name__, entrypoint)
        setattr(implementation, entrypoint.__name__, entrypoint)
    monkeypatch.setitem(sys.modules, "cloakbrowser", module)
    monkeypatch.setitem(sys.modules, "cloakbrowser.browser", implementation)
    monkeypatch.setattr(browser, "browser_capacity", capacity)
    monkeypatch.setattr(browser, "_require_browser_license", lambda *_args: None)
    monkeypatch.setattr(browser, "get_runtime_setting", lambda key, default=None: state.settings.get(key, default))
    browser.configure_browser_queue()
    yield state
    browser.BrowserSessionHelper.close_all_sessions()
    assert capacity.waiting == 0
    assert capacity.active == 0


def _until(predicate):
    """
    有界等待一个可观察状态，不用固定休眠判断竞态是否完成。
    """
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.001)


async def _until_async(predicate):
    """
    在保持事件循环可运行的情况下等待明确状态。
    """
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def test_owned_process_tree_rejects_foreign_root():
    """
    进程树只接受当前宿主直接创建的驱动，避免误杀其它浏览器。
    """
    with pytest.raises(RuntimeError, match="不能接管"):
        OwnedProcessTree(os.getpid(), "--moviepilot-browser-owner=foreign")


def test_owned_process_tree_kills_marked_reparented_child_without_touching_neighbor():
    """
    驱动退出后按唯一标记找回后代并结束，旁边的同类进程继续存活。
    """
    marker = "--moviepilot-browser-owner=test-tree"
    child_code = "import sys,time; time.sleep(60)"
    driver_code = (
        "import subprocess,sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]);"
    )
    neighbor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    driver = subprocess.Popen(
        [sys.executable, "-c", driver_code, child_code, marker],
    )
    tree = OwnedProcessTree(driver.pid, marker)
    try:
        assert driver.wait(timeout=3) == 0
        assert tree.terminate(2)
        assert tree.stopped
        assert neighbor.poll() is None
    finally:
        if driver.poll() is None:
            driver.kill()
        if neighbor.poll() is None:
            neighbor.kill()
        neighbor.wait(timeout=3)


def test_browser_watchdog_reclaims_owned_tree_after_operation_cancel(monkeypatch):
    """
    原生操作取消后由 owner 监控线程回收专属进程，并在确认后释放容量。
    """
    capacity = BrowserCapacity()
    lease = capacity.acquire(0.1)
    settings = {
        "CLOAKBROWSER_OPERATION_TIMEOUT": 1,
        "CLOAKBROWSER_CLOSE_TIMEOUT": 0.2,
    }
    monkeypatch.setattr(
        browser,
        "get_runtime_setting",
        lambda key, default=None: settings.get(key, default),
    )
    owner = browser._BrowserLaunchOwner(lease)
    tree = _WatchTree()
    owner._tree = tree
    owner.finish()
    cancelled = threading.Event()
    with browser_request(cancelled.is_set):
        owner.begin_operation()
    cancelled.set()
    owner._wake.set()
    assert owner.wait_stopped(2)
    assert tree.terminated
    assert capacity.active == 0


def test_delayed_native_start_is_killed_after_owner_expiry(monkeypatch):
    """
    启动超时后的迟到驱动会被新建的专属进程树立即结束，不能重新占用名额。
    """
    capacity = BrowserCapacity()
    lease = capacity.acquire(0.1)
    owner = browser._BrowserLaunchOwner(lease)
    owner._tree = _WatchTree()
    owner._tree.terminated = True
    owner._expired = True
    owner._reason = "启动已超时"
    candidate = _WatchTree()
    monkeypatch.setattr(browser, "OwnedProcessTree", lambda _pid, _marker: candidate)
    connection = MagicMock()
    connection._transport._proc.pid = 1234
    browser_type = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection))
    with pytest.raises(browser.BrowserOperationTimeoutError, match="启动已超时"):
        owner.attach(browser_type, {})
    assert candidate.terminated
    assert owner._stopped.is_set()
    assert capacity.active == 0


@pytest.mark.parametrize("name", ["launch", "launch_context", "launch_persistent_context"])
def test_sync_entrypoints_hold_one_seat_until_closed(provider, name):
    """
    三种同步入口及内部嵌套均只计一个实例，重复关闭不会扩大容量。
    """
    value = getattr(provider.module, name)()
    assert provider.capacity.active == 1
    assert len(provider.created) == 1
    assert getattr(provider.module, name) is getattr(provider.implementation, name)
    value.close()
    value.close()
    assert provider.capacity.active == 0


@pytest.mark.parametrize("name", ["launch_async", "launch_context_async", "launch_persistent_context_async"])
@pytest.mark.asyncio
async def test_async_entrypoints_hold_one_seat_until_closed(provider, name):
    """
    异步入口与内部嵌套沿用同一实例名额和关闭合同。
    """
    value = await getattr(provider.module, name)()
    assert provider.capacity.active == 1
    assert len(provider.created) == 1
    await value.close()
    await value.close()
    assert provider.capacity.active == 0


def test_host_sdk_and_legacy_requests_share_fifo_queue(provider):
    """
    宿主和直接 SDK 调用按入队顺序启动，持有者开新标签不需要新名额。
    """
    first = browser.launch_browser_context()
    first.new_page()
    first.new_page()
    with ThreadPoolExecutor(max_workers=2) as executor:
        second = executor.submit(browser.launch_browser_context)
        _until(lambda: provider.capacity.waiting == 1)
        third = executor.submit(provider.implementation.launch)
        _until(lambda: provider.capacity.waiting == 2)
        assert len(provider.created) == 1
        first.close()
        second_context = second.result(timeout=3)
        assert not third.done()
        second_context.close()
        third.result(timeout=3).close()
    assert len(provider.created) == 3


@pytest.mark.asyncio
async def test_sync_and_async_requests_share_capacity_without_blocking_loop(provider):
    """
    异步等待同步持有者释放时，事件循环和其他调用继续运行。
    """
    first = provider.module.launch()
    waiting = asyncio.create_task(browser.launch_browser_context_async())
    await _until_async(lambda: provider.capacity.waiting == 1)
    assert not waiting.done()
    assert len(provider.created) == 1
    first.close()
    second = await asyncio.wait_for(waiting, 3)
    await second.close()


def test_queue_timeout_does_not_start_or_release_other_browser(provider):
    """
    排队超时只撤销等待者，持有者和其名额都保持有效。
    """
    first = provider.module.launch()
    provider.settings["CLOAKBROWSER_QUEUE_TIMEOUT"] = 0.01
    try:
        with pytest.raises(TimeoutError, match="等待浏览器名额超时"):
            provider.module.launch()
        assert provider.capacity.active == 1
        assert provider.capacity.waiting == 0
        assert len(provider.created) == 1
    finally:
        first.close()


@pytest.mark.asyncio
async def test_cancelled_async_waiter_never_launches_after_capacity_returns(provider):
    """
    异步等待取消后立即出队，之后归还名额也不能补启动已取消的请求。
    """
    first = provider.module.launch()
    waiting = asyncio.create_task(provider.module.launch_async())
    await _until_async(lambda: provider.capacity.waiting == 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert provider.capacity.waiting == 0
    assert provider.capacity.active == 1
    first.close()
    await asyncio.sleep(0)
    assert len(provider.created) == 1


def test_cancelled_sync_request_is_removed_before_launch(provider):
    """
    线程内等待能观察调用取消，无需等占用中的另一个浏览器退出。
    """
    first = provider.module.launch()
    cancelled = threading.Event()

    def request():
        """
        为本次请求绑定可跨线程设置的取消信号。
        """
        with browser_request(cancelled.is_set):
            provider.module.launch()

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(request)
        _until(lambda: provider.capacity.waiting == 1)
        cancelled.set()
        with pytest.raises(BrowserQueueCancelledError):
            waiting.result(timeout=3)
    assert len(provider.created) == 1
    first.close()


@pytest.mark.asyncio
async def test_cancelling_inflight_launch_closes_result_before_releasing(provider):
    """
    启动已经进入 SDK 后取消，必须取得并关闭最终结果，不能留下后台浏览器。
    """
    provider.launch_started = asyncio.Event()
    provider.finish_launch = asyncio.Event()
    launching = asyncio.create_task(provider.module.launch_context_async())
    await provider.launch_started.wait()
    launching.cancel()
    await asyncio.sleep(0)
    assert not launching.done()
    assert provider.capacity.active == 1
    provider.finish_launch.set()
    with pytest.raises(asyncio.CancelledError):
        await launching
    assert len(provider.created) == 1
    assert provider.created[0].closed


@pytest.mark.asyncio
async def test_cancelling_close_keeps_seat_until_actual_exit(provider):
    """
    关闭协程取消不能提前释放仍在退出的实例，重复取消也等待真实终态。
    """
    value = await provider.module.launch_async()
    provider.close_started = asyncio.Event()
    provider.finish_close = asyncio.Event()
    closing = asyncio.create_task(value.close())
    await provider.close_started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    assert provider.capacity.active == 1
    assert not value.closed
    provider.finish_close.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert value.closed


@pytest.mark.parametrize("failure", ["fail_launch", "fail_context"])
def test_failed_launch_closes_partial_result_and_returns_seat(provider, failure):
    """
    SDK 启动或上下文初始化失败时，已创建的底层浏览器也必须关闭。
    """
    setattr(provider, failure, True)
    with pytest.raises(RuntimeError, match="failed"):
        provider.module.launch_context()
    assert all(value.closed for value in provider.created)
    assert provider.capacity.active == 0


def test_failed_close_retains_capacity_until_retry_succeeds(provider):
    """
    浏览器关闭失败仍占用名额，只有后续原句柄重试成功才允许新实例。
    """
    value = provider.module.launch()
    value.close_failures = 1
    with pytest.raises(RuntimeError, match="close failed"):
        value.close()
    assert provider.capacity.active == 1
    value.close()
    assert provider.capacity.active == 0
    provider.module.launch().close()


def test_browser_disconnect_returns_capacity_without_context_close(provider):
    """
    底层进程已经退出时，即使调用者尚未关闭上下文也能归还名额。
    """
    context = provider.module.launch_context()
    provider.created[0].disconnect()
    assert provider.capacity.active == 0
    context.close()


def test_closed_agent_owner_does_not_launch_after_wait(provider):
    """
    Agent 等待过程中所属任务封口后，不得再创建页面或占用新的实例名额。
    """
    first = provider.module.launch()
    owner = TerminalScope("test", "queued", "conversation")
    helper = browser.BrowserSessionHelper()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(helper.with_session, "queued", lambda state: state, owner=owner)
        _until(lambda: provider.capacity.waiting == 1)
        owner.seal()
        with pytest.raises(BrowserQueueCancelledError):
            pending.result(timeout=3)
    assert len(provider.created) == 1
    first.close()


@pytest.mark.asyncio
async def test_waiting_sessions_cannot_starve_current_browser_actions(provider):
    """
    多个会话等待唯一实例时，持有实例的会话仍可操作；等待取消后不遗留线程。
    """
    helper = browser.BrowserSessionHelper()
    await helper.run_action_async(
        "holder", lambda: helper.with_session("holder", lambda _state: "opened"),
    )
    pending = [
        asyncio.create_task(helper.run_action_async(
            f"wait-{index}", lambda index=index: helper.with_session(f"wait-{index}", lambda state: state),
        ))
        for index in range(3)
    ]
    await _until_async(lambda: provider.capacity.waiting == 3)
    try:
        result = await asyncio.wait_for(helper.run_action_async(
            "holder", lambda: helper.with_session("holder", lambda _state: "still usable"),
        ), 0.3)
        assert result == "still usable"
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await _until_async(lambda: provider.capacity.waiting == 0)
        await asyncio.to_thread(helper.close_session, "holder")
    assert len(provider.created) == 1
    await _until_async(lambda: not helper._session_executors)


@pytest.mark.asyncio
async def test_cancelled_same_session_waiter_does_not_reclaim_active_browser(provider):
    """
    同一会话已有动作时，后续等待者取消只撤销自己的排队任务。
    """
    helper = browser.BrowserSessionHelper()
    started = threading.Event()
    finish = threading.Event()

    def hold(_state):
        """
        保持会话线程占用，给第二个调用制造真实的会话内排队。
        """
        started.set()
        assert finish.wait(3)
        return "held"

    holder = asyncio.create_task(helper.run_action_async(
        "same-session", lambda: helper.with_session("same-session", hold),
    ))
    await _until_async(started.is_set)
    waiter = asyncio.create_task(helper.run_action_async(
        "same-session", lambda: helper.with_session("same-session", lambda _state: "waiter"),
    ))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert provider.created[0].closed is False
    finish.set()
    assert await holder == "held"
    await asyncio.to_thread(helper.close_session, "same-session")


def test_failed_page_initialization_and_close_retains_cleanup_owner(provider, monkeypatch):
    """
    页面初始化和首次关闭同时失败时，仍在原线程保留上下文供 owner 重试。
    """
    owner = object()
    context = provider.module.launch_context()
    context.new_page.side_effect = RuntimeError("page failed")
    provider.created[0].close_failures = 1
    monkeypatch.setattr(browser.BrowserSessionHelper, "_launch_context", staticmethod(lambda **_kw: context))
    helper = browser.BrowserSessionHelper()
    with pytest.raises(RuntimeError, match="close failed"):
        helper.with_session("broken", lambda state: state, owner=owner)
    assert provider.capacity.active == 1
    assert helper.close_owner(owner)
    assert not helper._failed_closures


def test_missing_key_does_not_start_sdk_or_create_signup_link(monkeypatch):
    """
    后台缺少 key 时只报告配置要求，不调用 SDK 的自动领取流程。
    """
    from cloakbrowser import license as provider_license

    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH", raising=False)
    resolve = MagicMock(return_value=None)
    monkeypatch.setattr(provider_license, "resolve_license_key", resolve)
    with pytest.raises(RuntimeError, match="CLOAKBROWSER_LICENSE_KEY"):
        browser._require_browser_license()
    resolve.assert_called_once_with(None)


def test_explicit_key_is_preserved_for_sdk_resolution(monkeypatch):
    """
    直接 SDK 调用传入 key 时继续优先使用其显式参数，不要求另配环境变量。
    """
    from cloakbrowser import license as provider_license

    resolve = MagicMock(return_value="test-key")
    monkeypatch.setattr(provider_license, "resolve_license_key", resolve)
    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH", raising=False)
    browser._require_browser_license("test-key")
    resolve.assert_called_once_with("test-key")


def test_legacy_positional_key_and_arguments_are_preserved(provider, monkeypatch):
    """
    旧插件用位置参数传 key 时，授权检查和 SDK 都收到原始值。
    """
    checked = MagicMock()
    monkeypatch.setattr(browser, "_require_browser_license", checked)

    def launch(headless=True, license_key=None):
        """
        模拟保留位置参数合同的 SDK 启动入口。
        """
        assert headless is False
        assert license_key == "test-key"
        return _Browser()

    wrapped = browser._queued_browser_launch(launch)
    wrapped(False, "test-key").close()
    checked.assert_called_once_with("test-key")


def test_sdk_copied_context_cannot_reuse_finished_launch_seat(provider):
    """
    SDK 在启动时保存的上下文不能让后续回调误用早已结束的嵌套启动资格。
    """
    captured = []

    def launch():
        """
        模拟 SDK 长期任务保存启动时上下文，返回正常浏览器句柄。
        """
        captured.append(copy_context())
        return _Browser()

    wrapped = browser._queued_browser_launch(launch)
    first = wrapped()
    provider.settings["CLOAKBROWSER_QUEUE_TIMEOUT"] = 0.01
    try:
        with pytest.raises(TimeoutError):
            captured[0].run(provider.module.launch)
        assert provider.capacity.active == 1
    finally:
        first.close()


def test_failed_agent_page_creation_releases_browser(provider):
    """
    新会话首次创建页面失败时关闭已经启动的上下文，不遗失唯一名额。
    """
    provider.fail_page = True
    with pytest.raises(RuntimeError, match="page failed"):
        browser.BrowserSessionHelper().with_session("failed-page", lambda state: state)
    assert provider.created[0].closed
    assert provider.capacity.active == 0


def test_agent_failed_close_is_retried_on_original_thread(provider):
    """
    显式关闭失败保留会话原执行器，任务回收可重试并释放唯一名额。
    """
    helper = browser.BrowserSessionHelper()
    owner = object()
    thread_id = helper.with_session("retry", lambda _state: threading.get_ident(), owner=owner)
    value = provider.created[0]
    value.close_failures = 2
    with pytest.raises(RuntimeError, match="close failed"):
        helper.close_session("retry", owner)
    assert provider.capacity.active == 1
    assert not helper.close_owner(owner)
    assert helper.close_owner(owner)
    assert not helper._failed_closures
    assert set(value.close_threads) == {thread_id}


def test_global_shutdown_retains_unclosed_browser_for_retry(provider):
    """
    全局关闭不能把尚未退出的实例当成成功，保留原 owner 后可再次收敛。
    """
    helper = browser.BrowserSessionHelper()
    helper.with_session("shutdown", lambda state: state)
    provider.created[0].close_failures = 2
    with pytest.raises(RuntimeError, match="部分浏览器尚未关闭"):
        helper.close_all_sessions()
    assert provider.capacity.active == 1
    helper.close_all_sessions()
    assert provider.capacity.active == 0


def test_startup_shutdown_closes_untracked_browser_owner(provider):
    """
    启动收尾会回收旧插件直接创建、未登记 Agent 会话的浏览器 owner。
    """
    instance = provider.module.launch()
    instance._moviepilot_browser_owner._tree = _WatchTree()
    modules_initializer.close_browser_sessions()
    assert instance._moviepilot_browser_owner._stopped.is_set()
    assert provider.capacity.active == 0


def test_startup_shutdown_reports_browser_owner_that_did_not_stop(provider, monkeypatch):
    """
    监控线程未收敛时启动收尾必须报告失败，避免静默释放未退出的资源。
    """
    monkeypatch.setattr(browser.BrowserSessionHelper, "close_all_sessions", MagicMock())
    monkeypatch.setattr(browser, "close_browser_instances", lambda: False)
    with pytest.raises(RuntimeError, match="监控线程尚未退出"):
        modules_initializer.close_browser_sessions()


@pytest.mark.asyncio
async def test_agent_tool_cancellation_reaches_waiting_browser_thread(provider):
    """
    浏览器工具的异步调用取消后，受保护的同步工作线程也会撤销队列请求。
    """
    first = provider.module.launch()
    tool = BrowseWebpageTool(session_id="cancelled-tool", user_id="test")
    pending = asyncio.create_task(tool._run_browser_action(
        browser_action="goto", url="https://example.com", selector=None, ref=None,
        value=None, script=None, content_type="text", timeout=30, cookies=None,
        user_agent=None, session_key="cancelled-tool", tab_index=None, allow_private_network=False,
    ))
    await _until_async(lambda: provider.capacity.waiting == 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await _until_async(lambda: provider.capacity.waiting == 0)
    first.close()
    assert len(provider.created) == 1


@pytest.mark.parametrize("method", ["action", "get_page_source"])
def test_site_page_close_failure_still_closes_instance(provider, method):
    """
    站点路径关闭单页失败时也关闭整个实例，防止其它调用一直等不到名额。
    """
    context = provider.module.launch_context()
    context.new_page.return_value.close.side_effect = RuntimeError("page close failed")
    with pytest.MonkeyPatch.context() as patch:
        # 复用一个已经准备好的替身，保持本用例只验证站点 finally 的关闭次序。
        patch.setattr(
            browser.PlaywrightHelper, "_PlaywrightHelper__launch_cloakbrowser_context",
            staticmethod(lambda **_kw: context),
        )
        helper = browser.PlaywrightHelper()
        if method == "action":
            helper.action("https://example.com", lambda _page: "ok", headless=True)
        else:
            helper.get_page_source("https://example.com", headless=True)
    assert provider.created[0].closed


def test_configured_capacity_allows_more_independent_instances(provider):
    """
    提高部署容量后可同时持有多个实例；归还仍按各自票据计数。
    """
    provider.settings["CLOAKBROWSER_MAX_CONCURRENCY"] = 2
    first = provider.module.launch()
    second = provider.module.launch_context()
    assert provider.capacity.active == 2
    first.close()
    assert provider.capacity.active == 1
    second.close()


def test_legacy_plugin_preparation_wraps_before_import(provider, tmp_path, monkeypatch):
    """
    旧插件导入 SDK 子模块前，加载准备入口已经安装同一套队列包装。
    """
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "__init__.py").write_text("from cloakbrowser.browser import launch\n", encoding="utf-8")
    monkeypatch.setattr(plugins_initializer, "acquire_managed_resource", lambda *_args, **_kwargs: None)
    original = provider.module.launch
    plugins_initializer._prepare_legacy_plugin_import(plugin_id="Legacy", plugin_dir=root)
    assert provider.module.launch is original
    namespace = {}
    exec((root / "__init__.py").read_text(), namespace)
    namespace["launch"]().close()
