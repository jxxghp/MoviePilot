"""
浏览器启动、可复用会话和站点页面访问适配，负责关闭所持有的页面与上下文。
"""

import asyncio
import inspect
import ipaddress
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from functools import partial, wraps
from typing import Any, Callable, Optional, Protocol, cast
from urllib.parse import urlparse

from app.adapters.network.http import RequestUtils, cookie_parse
from app.adapters.system.host import OwnedProcessTree
from app.runtime.browser import (
    BrowserLease,
    BrowserOperationTimeoutError,
    browser_cancel_check,
    browser_capacity,
    browser_request,
    check_browser_request,
)
from app.runtime.execution import await_task_to_terminal
from app.runtime.log import logger
from app.runtime.resources import (
    acquire_managed_resource,
    acquire_managed_resource_async,
)
from app.runtime.settings import get_runtime_setting
from app.runtime.tasks import TaskRegistry

_provider_queue_lock = threading.Lock()
_launch_owner: ContextVar[Optional["_BrowserLaunchOwner"]] = ContextVar("browser_launch_owner", default=None)
_browser_owners_lock = threading.RLock()
_browser_owners: set["_BrowserLaunchOwner"] = set()
_connection_owners: dict[Any, "_BrowserLaunchOwner"] = {}
_CLOAKBROWSER_ENTRYPOINTS = (
    "launch", "launch_async", "launch_context", "launch_context_async",
    "launch_persistent_context", "launch_persistent_context_async",
)


class _BrowserLaunchOwner:
    """
    把一次顶层启动及 SDK 内部嵌套启动绑定到同一个浏览器名额。
    """

    def __init__(self, lease: BrowserLease) -> None:
        """
        保存实例名额和启动期间已经取得的浏览器句柄。

        :param lease: 仅在浏览器关闭或无实例的启动失败后归还的名额
        """
        self._lease = lease
        self._resources: list[Any] = []
        self._starting = True
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._expired = False
        self._reason = ""
        self._tree: Optional[OwnedProcessTree] = None
        self._connections: set[Any] = set()
        self._marker = f"--moviepilot-browser-owner={uuid.uuid4().hex}"
        self._operation_timeout = float(get_runtime_setting("CLOAKBROWSER_OPERATION_TIMEOUT", 120))
        self._close_timeout = float(get_runtime_setting("CLOAKBROWSER_CLOSE_TIMEOUT", 5))
        self._startup_deadline = time.monotonic() + self._operation_timeout
        self._startup_cancelled = browser_cancel_check()
        self._close_deadline: Optional[float] = None
        self._operations: dict[object, tuple[float, Callable[[], bool]]] = {}
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._last_used = time.monotonic()
        # 每个真实实例只有一个监控线程；正常关闭与全局停机均显式收口。
        self._watcher = threading.Thread(target=self._watch, name="browser-watchdog", daemon=True)
        with _browser_owners_lock:
            _browser_owners.add(self)
        try:
            self._watcher.start()
        except BaseException:
            self._release()
            raise

    @property
    def starting(self) -> bool:
        """
        仅真实启动尚未结束时允许 SDK 内部嵌套调用复用名额。
        """
        return self._starting

    @property
    def expired(self) -> bool:
        """
        超时后的实例永久失效，不能通过迟到的 SDK 返回值重新启用。
        """
        return self._expired

    def check(self) -> None:
        """
        拒绝向已回收的实例派发新动作，保留动作结果可能未知的错误语义。
        """
        if self._expired:
            raise BrowserOperationTimeoutError(self._reason)

    def attach(self, browser_type: Any, kwargs: dict[str, Any]) -> None:
        """
        在内核出生前接管本次专属驱动，并给浏览器加入唯一进程标记。

        Playwright 的 PipeTransport 私有 PID 只在此边界读取；无法核实归属则拒绝启动。
        """
        connection = browser_type._impl_obj._connection
        pid = connection._transport._proc.pid
        release_after = False
        expired_error: Optional[BrowserOperationTimeoutError] = None
        with self._lock:
            known_connection = connection in self._connections
            if known_connection:
                assert self._tree is not None
                candidate_tree = self._tree
            else:
                candidate_tree = OwnedProcessTree(pid, self._marker)
            self._connections.add(connection)
            if self._tree is None or (self._expired and self._tree.stopped):
                self._tree = candidate_tree
            elif not known_connection and not self._expired:
                candidate_tree.terminate(self._close_timeout)
                raise RuntimeError("同一浏览器 owner 不能启动第二个原生驱动")
            with _browser_owners_lock:
                _connection_owners[connection] = self
            if self._expired:
                # 下载等步骤迟到后才创建驱动，也必须收掉，不能重新占用已归还的名额。
                if candidate_tree.terminate(self._close_timeout):
                    if candidate_tree is self._tree:
                        release_after = True
                expired_error = BrowserOperationTimeoutError(self._reason)
            if expired_error is None:
                kwargs["args"] = [*(kwargs.get("args") or []), self._marker]
                remaining_ms = max(1.0, (self._startup_deadline - time.monotonic()) * 1000)
                requested = kwargs.get("timeout") or remaining_ms
                kwargs["timeout"] = min(requested, remaining_ms)
        if release_after:
            self._release()
        if expired_error is not None:
            raise expired_error

    def begin_operation(self) -> object:
        """
        从当前请求捕获取消状态，给包括无原生超时的 evaluate 在内的 API 加硬期限。
        """
        with self._lock:
            if self._close_deadline is None:
                self.check()
            ticket = object()
            self._operations[ticket] = (
                time.monotonic() + self._operation_timeout, browser_cancel_check(),
            )
        self._wake.set()
        return ticket

    def end_operation(self, ticket: object) -> None:
        """
        只撤销本次 API 的期限，更新空闲时间而不延长其它在途操作。
        """
        with self._lock:
            self._operations.pop(ticket, None)
            self._last_used = time.monotonic()
        self._wake.set()

    def begin_close(self) -> None:
        """
        关闭只给一次宽限，嵌套 Browser/Context 关闭不能反复延长期限。
        """
        with self._lock:
            if self._close_deadline is None:
                self._close_deadline = time.monotonic() + self._close_timeout
        self._wake.set()

    def request_stop(self, reason: str = "浏览器操作被中断，实例已撤销") -> None:
        """
        由取消或停机边界要求监控线程回收，调用者不直接跨线程操作 Playwright。
        """
        with self._lock:
            self._expired = True
            self._reason = self._reason or reason
        self._wake.set()

    def _release(self) -> None:
        """
        已确认没有原生实例后归还容量，并保留到监控线程退出的有界 join。
        """
        self._stopped.set()
        self._wake.set()
        self._lease.release()
        with _browser_owners_lock:
            _browser_owners.discard(self)
            for connection, owner in tuple(_connection_owners.items()):
                if owner is self:
                    _connection_owners.pop(connection, None)
        if self._watcher.ident is not None and threading.current_thread() is not self._watcher:
            self._watcher.join(timeout=self._close_timeout)

    def _notify_connections(self) -> None:
        """
        把驱动强制退出传播到 Playwright 事件循环，唤醒正在等待的同步或异步 RPC。
        """
        with _browser_owners_lock:
            connections = tuple(
                connection for connection, owner in _connection_owners.items() if owner is self
            )
        for connection in connections:
            try:
                loop = connection._loop
                if not loop.is_closed():
                    loop.call_soon_threadsafe(self._cleanup_connection, connection, self._reason)
            except (AttributeError, RuntimeError, OSError) as error:
                logger.debug(f"通知浏览器连接退出失败：{error}")

    @staticmethod
    def _cleanup_connection(connection: Any, reason: str) -> None:
        """
        在 Playwright 所属事件循环收口连接，并读取无人等待的回调异常。

        被强制回收时可能同时存在初始化和业务 RPC；逐一读取已完成 Future
        只消除未处理告警，不改变仍在等待调用收到的异常。
        """
        callbacks = tuple(getattr(connection, "_callbacks", {}).values())
        try:
            connection.cleanup(reason)
        finally:
            futures = [callback.future for callback in callbacks]
            transport_error = getattr(getattr(connection, "_transport", None), "on_error_future", None)
            if transport_error is not None:
                futures.append(transport_error)
            for future in futures:
                if future.done():
                    try:
                        future.exception()
                    except BaseException:
                        pass
                else:
                    future.add_done_callback(_consume_future_exception)

    def stop_sync_connections(self) -> None:
        """
        在原生驱动已被回收后，从所属同步调用线程收口 Playwright dispatcher。

        强杀驱动不会安全地结束 Python greenlet；同步 close 需要显式 stop 以清理其事件循环。
        """
        for connection in tuple(self._connections):
            try:
                connection.stop_sync()
            except (AttributeError, RuntimeError, OSError, AssertionError) as error:
                logger.debug(f"收口同步浏览器连接失败：{error}")

    async def stop_async_connections(self) -> None:
        """
        在异步调用方收口已强制结束的 Playwright 连接，避免遗留协议任务。
        """
        for connection in tuple(self._connections):
            try:
                await asyncio.wait_for(connection.stop_async(), timeout=self._close_timeout)
            except (AttributeError, RuntimeError, OSError, AssertionError, asyncio.TimeoutError) as error:
                logger.debug(f"收口异步浏览器连接失败：{error}")

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """
        登记需要由当前 owner 观察的异步启动或关闭任务，避免超时后变成无主任务。
        """
        with self._lock:
            self._owned_tasks.add(task)

        def forget(done: asyncio.Task[Any]) -> None:
            """
            任务进入终态后从 owner 的生命周期账本移除。
            """
            with self._lock:
                self._owned_tasks.discard(done)

        task.add_done_callback(forget)
        return task

    async def wait_task(self, task: asyncio.Task[Any], timeout: float) -> tuple[bool, bool]:
        """
        有界等待 owner 任务；重复取消只记录并在任务终态后交回调用方。

        :param task: 已由 :meth:`track_task` 登记的任务
        :param timeout: 最长等待秒数
        :return: ``(是否取得终态, 调用方是否收到取消)``
        """
        deadline = time.monotonic() + timeout
        cancelled = False
        while not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, cancelled
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
            except asyncio.TimeoutError:
                return False, cancelled
        return True, cancelled

    def closed(self) -> None:
        """
        正常关闭后核对原生终态；驱动仍在时交回监控线程继续确认。
        """
        if self._tree is None or self._tree.stopped:
            self._release()
        else:
            self.begin_close()

    def wait_stopped(self, timeout: float) -> bool:
        """
        停机只有限等待；未收敛的 owner 和容量继续保留以供重试。
        """
        self._watcher.join(timeout=timeout)
        return not self._watcher.is_alive() and self._stopped.is_set()

    def _expiry_reason(self) -> str:
        """
        在锁内计算最早失效原因，空闲会话也有最终回收期限。
        """
        now = time.monotonic()
        if self._starting and (self._startup_cancelled() or now >= self._startup_deadline):
            return "浏览器启动超时或被中断，实例已撤销"
        if self._close_deadline is not None and now >= self._close_deadline:
            return "浏览器关闭超时，正在强制回收本次实例"
        for deadline, cancelled in self._operations.values():
            if cancelled() or now >= deadline:
                return "浏览器操作超时或被中断，实际执行结果未知，实例已撤销"
        if not self._starting and not self._operations and now - self._last_used >= 15 * 60:
            return "浏览器空闲超过 15 分钟，实例已回收"
        return ""

    def _watch(self) -> None:
        """
        独立于卡住的 RPC 和事件循环回收专属进程，确认退出后才放行下一实例。
        """
        while not self._stopped.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            with self._lock:
                reason = self._reason or self._expiry_reason()
                if not reason:
                    continue
                self._expired = True
                self._reason = reason
                tree = self._tree
                has_resource = bool(self._resources)
            if tree is not None:
                if not tree.terminate(self._close_timeout):
                    # 权限/内核异常不能假报名额空闲；保留 owner 供停机重试。
                    self._wake.wait(self._close_timeout)
                    continue
                self._notify_connections()
                self._release()
                continue
            elif has_resource:
                continue
            else:
                # 启动尚未拿到驱动时，先保留名额到硬启动期限；原生 guard
                # 会拒绝期限后的迟到启动，避免空档期间产生无主浏览器。
                with self._lock:
                    if self._starting and time.monotonic() < self._startup_deadline:
                        continue
                self._release()

    def finish(self) -> None:
        """
        撤销嵌套启动资格，防止 SDK 子任务继承的上下文日后绕过队列。
        """
        self._starting = False
        self._wake.set()

    async def _close_async_resource(
        self, original_close: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> Any:
        """
        在关闭宽限内等待异步句柄，超时后交给 owner 监控线程回收。
        """
        if self.expired and self._stopped.is_set():
            await self.stop_async_connections()
            return None
        self.begin_close()
        task = self.track_task(asyncio.create_task(original_close(*args, **kwargs)))
        try:
            completed, cancelled = await self.wait_task(task, self._close_timeout)
        except Exception:
            if not (self.expired and self._stopped.is_set()):
                raise
            completed, cancelled = True, False
        if not completed:
            self.request_stop("浏览器关闭超时，实际状态未知，实例已撤销")
            self.closed()
            if cancelled:
                raise asyncio.CancelledError
            raise BrowserOperationTimeoutError("浏览器关闭超时，实际状态未知")
        try:
            result = task.result()
        except Exception:
            if not (self.expired and self._stopped.is_set()):
                raise
            result = None
        self.closed()
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _close_sync_resource(
        self, original_close: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> Any:
        """
        在所属同步线程关闭句柄；失败时保留 owner 供监控和后续收口重试。
        """
        if self.expired and self._stopped.is_set():
            self.stop_sync_connections()
            return None
        self.begin_close()
        try:
            result = original_close(*args, **kwargs)
        except Exception:
            if not (self.expired and self._stopped.is_set()):
                raise
            result = None
        self.closed()
        return result

    def bind(self, resource: Any, asynchronous: bool) -> Any:
        """
        保留原对象身份和关闭参数，在关闭成功或进程断开后幂等归还名额。

        :param resource: SDK 返回的 Browser 或 BrowserContext
        :param asynchronous: 该对象是否使用异步关闭方法
        :return: 原浏览器对象
        """
        self._resources.append(resource)
        setattr(resource, "_moviepilot_browser_owner", self)
        original_close = resource.close
        if asynchronous:
            @wraps(original_close)
            async def close_async(*args: Any, **kwargs: Any) -> Any:
                """
                调用 owner 的有界异步关闭实现。
                """
                return await self._close_async_resource(original_close, *args, **kwargs)

            resource.close = close_async
        else:
            @wraps(original_close)
            def close_sync(*args: Any, **kwargs: Any) -> Any:
                """
                调用 owner 的同步关闭实现。
                """
                return self._close_sync_resource(original_close, *args, **kwargs)

            resource.close = close_sync

        # 普通 Context 的 close 事件可能早于其 Browser 真正退出，不能据此归还。
        target = getattr(resource, "browser", None) or resource
        if callable(getattr(target, "is_connected", None)):
            target.on("disconnected", lambda *_args: self.closed())
        return resource

    def abort(self) -> None:
        """
        启动失败或取消后关闭已取得的同步句柄；关闭失败保留真实占用。
        """
        if not self._resources:
            self.closed()
        for resource in reversed(self._resources):
            try:
                resource.close()
            except Exception as error:
                logger.warning(f"回收未完成启动的浏览器失败：{error}")

    async def abort_async(self) -> None:
        """
        等待异步启动清理取得终态，即使调用方再次取消也不遗留新实例。
        """
        if not self._resources:
            self.closed()
        for resource in reversed(self._resources):
            task = self.track_task(asyncio.create_task(resource.close()))
            try:
                completed, _cancelled = await self.wait_task(task, self._close_timeout)
                if not completed:
                    self.request_stop("浏览器启动清理超时，实例已撤销")
                    self.closed()
                    raise BrowserOperationTimeoutError("浏览器启动清理超时，实际状态未知")
                task.result()
            except Exception as error:
                logger.warning(f"回收未完成启动的浏览器失败：{error}")


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """
    读取驱动 EOF 之后才完成的内部 Future，避免强制回收制造告警。
    """
    try:
        future.exception()
    except BaseException:
        pass


def _browser_queue_timeout() -> float:
    """
    读取部署侧浏览器容量和排队秒数，降容不会中断正在使用的实例。
    """
    browser_capacity.configure(int(get_runtime_setting("CLOAKBROWSER_MAX_CONCURRENCY", 3)))
    return float(get_runtime_setting("CLOAKBROWSER_QUEUE_TIMEOUT", 60))


def _require_browser_license(license_key: Optional[str] = None) -> None:
    """
    兼容显式 key 解析，但保留 0.6.5 无 key 也可运行的免费内核路径。

    :param license_key: SDK 调用者显式传入的 key，优先于 SDK 的环境变量和本地文件
    """
    if os.environ.get("CLOAKBROWSER_BINARY_PATH"):
        return
    from cloakbrowser.license import resolve_license_key

    # 0.6.5 的 key 只用于选择最新或 Pro 内核，缺少 key 时由 SDK 回退到旧版免费内核。
    resolve_license_key(license_key)


def _explicit_browser_key(
    original: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any],
) -> Optional[str]:
    """
    读取 SDK 显式 key，兼容旧插件的位置参数调用且不修改原始参数。
    """
    if "license_key" in kwargs:
        return cast(Optional[str], kwargs["license_key"])
    parameters = tuple(inspect.signature(original).parameters)
    if "license_key" in parameters:
        position = parameters.index("license_key")
        if len(args) > position:
            return cast(Optional[str], args[position])
    return None


def _guard_native_launch(original: Callable[..., Any], asynchronous: bool) -> Callable[..., Any]:
    """
    仅在 CloakBrowser 启动上下文中接管专属驱动，普通 Playwright 调用保持原样。
    """
    @wraps(original)
    def launch(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
        """
        在向原生驱动发送启动指令前登记归属、标记和剩余启动期限。
        """
        owner = _launch_owner.get()
        if owner is not None and (owner.starting or owner.expired):
            owner.attach(browser_type, kwargs)
        return original(browser_type, *args, **kwargs)

    @wraps(original)
    async def launch_async(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
        """
        异步内核启动也经过同一归属检查，不允许取消后的迟到启动。
        """
        owner = _launch_owner.get()
        if owner is not None and (owner.starting or owner.expired):
            owner.attach(browser_type, kwargs)
        return await original(browser_type, *args, **kwargs)

    wrapped = launch_async if asynchronous else launch
    setattr(wrapped, "_moviepilot_guarded", True)
    return wrapped


def _guard_native_calls(original: Callable[..., Any]) -> Callable[..., Any]:
    """
    在 Playwright 连接边界监控所有外层 API，覆盖没有原生 timeout 的脚本操作。
    """
    @wraps(original)
    async def call(connection: Any, *args: Any, **kwargs: Any) -> Any:
        """
        无论同步 greenlet 还是异步 Task，都按连接身份找到实例 owner。
        """
        with _browser_owners_lock:
            owner = _connection_owners.get(connection)
        if owner is None or connection._api_zone.get():
            return await original(connection, *args, **kwargs)
        ticket = owner.begin_operation()
        try:
            return await original(connection, *args, **kwargs)
        except Exception:
            owner.check()
            raise
        finally:
            owner.end_operation(ticket)

    setattr(call, "_moviepilot_guarded", True)
    return call


def _guard_native_calls_sync(original: Callable[..., Any]) -> Callable[..., Any]:
    """
    同步 Playwright 通过 greenlet 进入同一监控边界，覆盖 evaluate 等无限等待调用。
    """
    @wraps(original)
    def call(connection: Any, *args: Any, **kwargs: Any) -> Any:
        """
        按连接身份登记同步 API 期限，保留未被 MoviePilot 接管的连接原行为。
        """
        with _browser_owners_lock:
            owner = _connection_owners.get(connection)
        if owner is None or connection._api_zone.get():
            return original(connection, *args, **kwargs)
        ticket = owner.begin_operation()
        try:
            return original(connection, *args, **kwargs)
        except BaseException:
            owner.check()
            raise
        finally:
            owner.end_operation(ticket)

    setattr(call, "_moviepilot_guarded", True)
    return call


def _configure_native_browser_guard() -> None:
    """
    惰性安装固定 Playwright 版本的原生边界钩子；不创建浏览器或驱动。
    """
    from playwright._impl._connection import Connection
    from playwright.async_api import BrowserType as AsyncBrowserType
    from playwright.sync_api import BrowserType as SyncBrowserType

    for browser_type, asynchronous in ((SyncBrowserType, False), (AsyncBrowserType, True)):
        for name in ("launch", "launch_persistent_context"):
            original = getattr(browser_type, name)
            if getattr(original, "_moviepilot_guarded", False) is not True:
                setattr(browser_type, name, _guard_native_launch(original, asynchronous))
    original_call = Connection.wrap_api_call
    if getattr(original_call, "_moviepilot_guarded", False) is not True:
        Connection.wrap_api_call = _guard_native_calls(original_call)
    original_sync_call = Connection.wrap_api_call_sync
    if getattr(original_sync_call, "_moviepilot_guarded", False) is not True:
        Connection.wrap_api_call_sync = _guard_native_calls_sync(original_sync_call)


def close_browser_instances() -> bool:
    """
    停机时收口包含旧插件在内的所有实例监控线程，未退出的 owner 保留并返回失败。
    """
    with _browser_owners_lock:
        owners = tuple(_browser_owners)
    for owner in owners:
        owner.request_stop("系统正在停止，浏览器实例已撤销")
    deadline = time.monotonic() + float(get_runtime_setting("CLOAKBROWSER_CLOSE_TIMEOUT", 5)) + 1
    results = [owner.wait_stopped(max(0, deadline - time.monotonic())) for owner in owners]
    return all(results)


def _browser_close_budget() -> float:
    """
    给原生强制回收和 RPC 终态传播保留少量余量，关闭等待始终有界。
    """
    return float(get_runtime_setting("CLOAKBROWSER_CLOSE_TIMEOUT", 5)) + 1


def _browser_action_budget() -> float:
    """
    限制整个宿主回调等待，无法中止的第三方 Python 代码仍保留原执行 owner。
    """
    return (
        float(get_runtime_setting("CLOAKBROWSER_QUEUE_TIMEOUT", 60))
        + float(get_runtime_setting("CLOAKBROWSER_OPERATION_TIMEOUT", 120))
        + _browser_close_budget()
    )


def _context_browser_owner(context: Any) -> Optional[_BrowserLaunchOwner]:
    """
    只识别本适配器绑定的真实 owner，避免旧插件替身动态属性造成误判。
    """
    owner = getattr(context, "_moviepilot_browser_owner", None)
    return owner if isinstance(owner, _BrowserLaunchOwner) else None


def _queued_browser_launch(original: Callable[..., Any]) -> Callable[..., Any]:
    """
    包装同步入口，名额跨越 SDK 返回后的完整实例存活期。
    """
    @wraps(original)
    def launch(*args: Any, **kwargs: Any) -> Any:
        """
        嵌套 SDK 启动共享顶层名额，外部独立启动按同一队列等待。
        """
        owner = _launch_owner.get()
        if owner is not None and owner.starting:
            return owner.bind(original(*args, **kwargs), asynchronous=False)
        _require_browser_license(_explicit_browser_key(original, args, kwargs))
        owner = _BrowserLaunchOwner(browser_capacity.acquire(_browser_queue_timeout()))
        token = _launch_owner.set(owner)
        try:
            check_browser_request()
            result = owner.bind(original(*args, **kwargs), asynchronous=False)
            owner.check()
            check_browser_request()
            return result
        except BaseException:
            owner.finish()
            owner.abort()
            raise
        finally:
            owner.finish()
            _launch_owner.reset(token)

    return launch


def _queued_browser_launch_async(original: Callable[..., Any]) -> Callable[..., Any]:
    """
    包装异步入口，取消已经开始的启动时先等到终态并关闭结果。
    """
    @wraps(original)
    async def launch(*args: Any, **kwargs: Any) -> Any:
        """
        异步等待不占同步线程，SDK 内部启动仍复用同一个名额。
        """
        owner = _launch_owner.get()
        if owner is not None and owner.starting:
            return owner.bind(await original(*args, **kwargs), asynchronous=True)
        _require_browser_license(_explicit_browser_key(original, args, kwargs))
        lease = await browser_capacity.acquire_async(_browser_queue_timeout())
        owner = _BrowserLaunchOwner(lease)
        token = _launch_owner.set(owner)

        async def invoke() -> Any:
            """
            启动前复核取消，成功后登记句柄以支持取消路径回收。
            """
            check_browser_request()
            return owner.bind(await original(*args, **kwargs), asynchronous=True)

        task = owner.track_task(asyncio.create_task(invoke()))
        try:
            result = await asyncio.shield(task)
            owner.check()
            check_browser_request()
            return result
        except BaseException:
            owner.request_stop("浏览器启动失败或被取消，实例已撤销")
            try:
                completed, _cancelled = await owner.wait_task(
                    task, owner._operation_timeout + owner._close_timeout,
                )
                if completed:
                    task.result()
            except BaseException:
                pass
            owner.finish()
            await owner.abort_async()
            raise
        finally:
            owner.finish()
            _launch_owner.reset(token)

    return launch


def configure_browser_queue() -> None:
    """
    在首次宿主启动或旧插件导入前统一包装 CloakBrowser 六类公开入口。

    包根和 browser 子模块共享包装对象；SDK 内部的 launch 嵌套不会重复取号。
    """
    import cloakbrowser

    modules = (cloakbrowser, sys.modules.get("cloakbrowser.browser"))
    with _provider_queue_lock:
        _configure_native_browser_guard()
        wrappers: dict[int, Callable[..., Any]] = {}
        for module in modules:
            if module is None:
                continue
            for name in _CLOAKBROWSER_ENTRYPOINTS:
                original = getattr(module, name, None)
                if original is None or getattr(original, "_moviepilot_queued", False) is True:
                    continue
                wrapped = wrappers.get(id(original))
                if wrapped is None:
                    factory = _queued_browser_launch_async if name.endswith("_async") else _queued_browser_launch
                    wrapped = factory(original)
                    setattr(wrapped, "_moviepilot_queued", True)
                    wrappers[id(original)] = wrapped
                setattr(module, name, wrapped)


class BrowserElement(Protocol):
    """
    页面元素的最小接口，避免为了类型标注直接导入 Playwright。
    """

    def is_visible(self) -> bool:
        """
        检查当前元素是否可见。

        :return: 元素满足浏览器可见性规则时返回 True，否则返回 False
        """
        ...

    def fill(self, value: str) -> None:
        """
        替换元素中的文本内容。

        :param value: 要填写的完整文本；空字符串用于清空内容
        """
        ...

    def inner_text(self) -> str:
        """
        读取元素渲染后的可见文本。

        :return: 当前元素的可见文本，允许为空字符串
        """
        ...


class BrowserContext(Protocol):
    """
    CloakBrowser 返回的上下文只需要满足这些能力即可。
    """

    def new_page(self) -> "BrowserPage":
        """
        在当前上下文中创建页面。

        :return: 新页面，由调用方或上下文关闭流程释放
        """
        ...

    def cookies(self) -> list[dict[str, Any]]:
        """
        读取上下文当前保存的 Cookie。

        :return: 浏览器 Cookie 对象列表，没有 Cookie 时为空列表
        """
        ...

    def close(self) -> None:
        """
        关闭上下文及其页面，释放该上下文持有的浏览器资源。
        """
        ...


class BrowserPage(Protocol):
    """
    同步浏览器页面接口，覆盖页面会话和站点登录所需能力。

    实现方沿用浏览器的超时和异常行为；页面只可在所属上下文和线程仍有效时使用。

    :ivar context: 创建此页面的浏览器上下文，负责页面共享会话状态
    :ivar url: 当前页面地址，导航后随页面状态更新
    """

    context: BrowserContext
    url: str

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        """
        设置此页面后续请求使用的额外 HTTP 请求头。

        :param headers: 请求头名称与文本值的映射
        """
        ...

    def set_default_timeout(self, timeout: int) -> None:
        """
        设置页面操作的默认等待上限。

        :param timeout: 超时时间，单位为毫秒；0 沿用浏览器禁用超时的语义
        """
        ...

    def goto(self, url: str, *args: Any, **kwargs: Any) -> Any:
        """
        导航到目标地址并按指定条件等待加载。

        :param url: 目标页面地址
        :param args: 导航接口保留的位置参数
        :param kwargs: 导航等待条件、超时（毫秒）或来源页等选项
        :return: 最终导航响应对象；没有发生可返回响应的导航时可能为 None
        """
        ...

    def wait_for_load_state(self, state: str, *args: Any, **kwargs: Any) -> Any:
        """
        等待页面达到指定加载状态。

        :param state: 浏览器加载状态，例如 load、domcontentloaded 或 networkidle
        :param args: 加载等待接口保留的位置参数
        :param kwargs: 等待超时（毫秒）等加载选项
        :return: 等待完成后不返回业务数据；超时沿用浏览器异常
        """
        ...

    def wait_for_selector(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """
        等待选择器对应元素达到指定状态。

        :param selector: 定位目标元素的选择器
        :param args: 元素等待接口保留的位置参数
        :param kwargs: 目标状态、严格匹配或超时（毫秒）等选项
        :return: 匹配元素；等待隐藏或移除状态时可能为 None
        """
        ...

    def fill(self, selector: str, value: str, *args: Any, **kwargs: Any) -> Any:
        """
        定位输入元素并替换文本。

        :param selector: 定位输入元素的选择器
        :param value: 要填入的完整文本，空字符串用于清空
        :param args: 文本填写接口保留的位置参数
        :param kwargs: 强制执行、严格匹配或超时（毫秒）等选项
        :return: 操作完成后不返回业务数据；定位或填写失败沿用浏览器异常
        """
        ...

    def click(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """
        定位元素并执行点击。

        :param selector: 定位可点击元素的选择器
        :param args: 点击接口保留的位置参数
        :param kwargs: 鼠标按键、点击次数、位置或超时（毫秒）等选项
        :return: 操作完成后不返回业务数据；定位或点击失败沿用浏览器异常
        """
        ...

    def select_option(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """
        设置下拉框选项。

        :param selector: 定位下拉框的选择器
        :param args: 传给下拉框接口的选项值
        :param kwargs: 选项值、标签、索引或超时（毫秒）等选项
        :return: 选中选项的值列表，沿用浏览器接口的返回格式
        """
        ...

    def query_selector(self, selector: str) -> Optional[BrowserElement]:
        """
        查找首个匹配选择器的元素。

        :param selector: 浏览器支持的元素选择器
        :return: 首个匹配元素；没有匹配项时返回 None
        """
        ...

    def title(self) -> str:
        """
        读取当前页面标题。

        :return: 页面标题文本，允许为空字符串
        """
        ...

    def inner_text(self, selector: str) -> str:
        """
        读取选择器对应元素的可见文本。

        :param selector: 定位目标元素的选择器
        :return: 目标元素的可见文本；定位失败沿用浏览器异常
        """
        ...

    def content(self) -> str:
        """
        读取当前页面完整 HTML 源码。

        :return: 包含文档类型声明的页面源码；导航期间读取可能抛出浏览器异常
        """
        ...

    def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """
        在当前页面中执行 JavaScript 表达式。

        :param expression: 待执行的 JavaScript 表达式或函数文本
        :param args: 传入 JavaScript 函数的参数
        :param kwargs: 浏览器表达式执行接口接受的关键字参数
        :return: 表达式结果，按浏览器接口的序列化规则转换为 Python 对象
        """
        ...

    def screenshot(self, *args: Any, **kwargs: Any) -> bytes:
        """
        截取当前页面图像。

        :param args: 截图接口保留的位置参数
        :param kwargs: 图像格式、质量、全页范围或超时（毫秒）等选项
        :return: 截图的二进制内容，格式由请求选项决定
        """
        ...

    def close(self) -> None:
        """
        关闭当前页面，所属上下文及其他页面保持有效。
        """
        ...


def launch_browser_context(headless: bool = True, **kwargs: Any) -> BrowserContext:
    """
    排队启动同步浏览器上下文；关闭上下文后才归还实例名额。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    if not headless:
        acquire_managed_resource(
            "host.display",
            reason="headed_browser_launch",
            retry=True,
        )
    configure_browser_queue()
    from cloakbrowser import launch_context

    return cast(BrowserContext, launch_context(headless=headless, **kwargs))


async def launch_browser_context_async(
    headless: bool = True,
    **kwargs: Any,
) -> Any:
    """
    排队启动异步浏览器上下文；取消启动时回收已经创建的实例。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    if not headless:
        await acquire_managed_resource_async(
            "host.display",
            reason="headed_browser_launch",
            retry=True,
        )
    configure_browser_queue()
    from cloakbrowser import launch_context_async

    return await launch_context_async(headless=headless, **kwargs)


@dataclass
class _BrowserSessionState:
    """
    保存可复用浏览器上下文、页面游标与会话资源归属。

    :param session_key: 会话唯一键，用于索引复用会话和专属工作线程
    :param context: 会话持有的浏览器上下文，销毁会话时关闭
    :param pages: 会话打开的页面列表
    :param active_index: 当前活动页在 pages 中的索引，默认 0
    :param user_agent: 创建会话时使用的浏览器标识；None 表示使用实现默认值
    :param cookies: 用于会话的 Cookie 请求头文本；None 表示未额外指定
    :param owner: 持有会话资源的运行时登记对象；None 表示未登记
    :param created_at: 创建时间，使用单调时钟的秒值
    :param last_used_at: 最近操作完成时间，使用单调时钟的秒值，失败操作同样更新时间
    :param lock: 保护会话状态和页面操作的可重入锁，默认每个会话独立创建
    """

    session_key: str
    context: BrowserContext
    pages: list[BrowserPage]
    active_index: int = 0
    user_agent: Optional[str] = None
    cookies: Optional[str] = None
    owner: Optional[Any] = field(default=None, repr=False)
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def active_page(self) -> BrowserPage:
        """
        取得活动索引对应的页面。

        :return: 当前活动页面；调用方需保证页面列表非空且索引有效
        """
        return self.pages[self.active_index]


class BrowserSessionHelper:
    """
    Agent 浏览器会话辅助类，负责复用 CloakBrowser 上下文并生成可操作页面快照。
    """

    SESSION_TTL_SECONDS = 15 * 60
    # 每分钟扫描一次，不依赖下一次浏览器请求触发空闲回收。
    REAP_INTERVAL_SECONDS = 60
    MAX_SESSIONS = 8
    DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
    PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal")
    PRIVATE_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback"}
    REF_ATTRIBUTE = "data-moviepilot-agent-ref"
    SESSION_WORKER_NAME_PREFIX = "browser-session"

    _sessions: dict[str, _BrowserSessionState] = {}
    _session_executors: dict[str, ThreadPoolExecutor] = {}
    _session_executor_owners: dict[str, Any] = {}
    _session_thread_ids: dict[str, int] = {}
    _sessions_lock = threading.RLock()
    # 按执行器身份统计已接受的排队及在途操作，不能把排队时间误认为空闲。
    _pending_operations: dict[ThreadPoolExecutor, int] = {}
    _idle_closures: dict[ThreadPoolExecutor, Future[bool]] = {}
    _idle_close_owners: dict[ThreadPoolExecutor, Any] = {}
    # 显式关闭撤销准入后仍保留真实终态，供 owner 和全量关闭共同等待。
    _explicit_closures: dict[ThreadPoolExecutor, tuple[Any, Future[bool]]] = {}
    # 关闭失败必须保留原线程和句柄，不能丢弃真实占用或在其它线程重试 Playwright。
    _failed_closures: dict[ThreadPoolExecutor, _BrowserSessionState] = {}
    _reaper_task: Optional[asyncio.Task[None]] = None

    @classmethod
    def start_idle_reaper(cls, tasks: TaskRegistry) -> None:
        """
        在宿主事件循环中启动唯一的周期回收任务，不预先创建浏览器或工作线程。

        :param tasks: 当前应用生命周期持有的后台任务登记器
        """
        if cls._reaper_task is None or cls._reaper_task.done():
            cls._reaper_task = tasks.create(
                cls._reap_loop(), owner="browser.sessions.idle_reaper",
            )

    @classmethod
    async def _reap_loop(cls) -> None:
        """
        周期投递空闲检查；取消只停止扫描，并等待已经投递的关闭达到终态。

        全量关闭仍由模块关闭阶段负责，不能在消费者停止前关闭活动会话。
        """
        try:
            while True:
                await asyncio.sleep(cls.REAP_INTERVAL_SECONDS)
                cls._prune_sessions()
        finally:
            with cls._sessions_lock:
                closures = tuple(cls._idle_closures.values())
            for future in closures:
                try:
                    await await_task_to_terminal(asyncio.wrap_future(future))
                except Exception as err:
                    logger.warning(f"等待空闲浏览器关闭失败：{err}")

    def __init__(self, headless: bool = True, viewport: Optional[dict[str, int]] = None):
        """
        初始化浏览器会话辅助类。

        :param headless: 是否使用无头浏览器
        :param viewport: 默认视口大小
        """
        self.headless = headless
        self.viewport = viewport or self.DEFAULT_VIEWPORT

    @classmethod
    def validate_url(cls, url: str, allow_private_network: bool = False) -> str:
        """
        校验浏览器可访问的 URL，默认拒绝本机、私网和非 HTTP 协议。

        :param url: 待访问的 URL
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 原始 URL
        """
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("仅支持 http/https URL")
        if not parsed.hostname:
            raise ValueError("URL 缺少主机名")

        hostname = parsed.hostname.lower().rstrip(".")
        if allow_private_network:
            return url

        if hostname in cls.PRIVATE_HOSTNAMES or hostname.endswith(
            cls.PRIVATE_HOST_SUFFIXES
        ):
            raise ValueError("默认不允许访问本机或私网地址")

        try:
            ip_address = ipaddress.ip_address(hostname)
        except ValueError:
            return url

        if not ip_address.is_global:
            raise ValueError("默认不允许访问本机或私网地址")
        return url

    @classmethod
    def ref_to_selector(cls, ref: str) -> str:
        """
        将页面快照中的元素引用转换为稳定选择器。

        :param ref: 快照返回的元素引用
        :return: 可传给浏览器的属性选择器
        """
        clean_ref = (ref or "").strip()
        if not clean_ref:
            raise ValueError("元素 ref 不能为空")
        escaped_ref = clean_ref.replace("\\", "\\\\").replace('"', '\\"')
        return f'[{cls.REF_ATTRIBUTE}="{escaped_ref}"]'

    @classmethod
    def close_all_sessions(cls) -> None:
        """
        关闭所有 Agent 浏览器会话。
        """
        with cls._sessions_lock:
            session_keys = set(cls._sessions) | set(cls._session_executors)
        for session_key in session_keys:
            with cls._sessions_lock:
                session = cls._sessions.get(session_key)
                owner = session.owner if session else cls._session_executor_owners.get(session_key)
            try:
                cls.close_session(session_key, owner=owner)
            except Exception as error:
                logger.warning(f"关闭浏览器会话失败，保留原线程重试：{error}")
        with cls._sessions_lock:
            closures = tuple(cls._idle_closures.values()) + tuple(
                future for _owner, future in cls._explicit_closures.values()
            )
        for future in closures:
            future.result(timeout=_browser_close_budget())
        if not cls._retry_failed_sessions(all_owners=True):
            raise RuntimeError("部分浏览器尚未关闭，不能释放其运行资源")

    @classmethod
    def close_session(cls, session_key: str, owner: Optional[Any] = None) -> bool:
        """
        关闭指定 Agent 浏览器会话。

        :param session_key: 会话标识
        :param owner: 可选宿主作用域；提供时只允许关闭该作用域拥有的会话
        :return: 找到并关闭会话时返回 True
        """
        if cls._is_current_session_thread(session_key):
            return cls._close_session_in_thread(session_key, owner=owner)

        with cls._sessions_lock:
            executor = cls._get_existing_session_executor(session_key, owner=owner)
            session = cls._sessions.get(session_key)
            browser_owner = _context_browser_owner(session.context) if session else None
            if browser_owner is not None:
                browser_owner.begin_close()
            if executor is None:
                failed = any(
                    session.session_key == session_key and session.owner is owner
                    for session in cls._failed_closures.values()
                )
                if not failed:
                    return False
        if executor is None:
            return cls._retry_failed_sessions(owner=owner, session_key=session_key)
        with cls._sessions_lock:
            context = copy_context()

            def close_in_context() -> bool:
                """
                在原会话线程执行关闭，完成后释放准入计数。
                """
                return bool(context.run(
                    cls._run_session_task, session_key, cls._close_session_in_thread,
                    session_key, owner=owner,
                ))

            cls._pending_operations[executor] = cls._pending_operations.get(executor, 0) + 1
            try:
                future: Future[bool] = Context().run(executor.submit, close_in_context)
                future.add_done_callback(partial(cls._release_operation, executor))
            except RuntimeError:
                cls._release_operation(executor)
                raise
        result = future.result(timeout=_browser_close_budget())
        cls._shutdown_session_executor(session_key, expected_executor=executor)
        return result

    @classmethod
    def close_owner(cls, owner: Any) -> bool:
        """
        关闭宿主作用域创建的全部浏览器会话和其专属执行线程。
        """
        with cls._sessions_lock:
            session_keys = {
                key for key, session in cls._sessions.items() if session.owner is owner
            }
            session_keys.update(
                key for key, session_owner in cls._session_executor_owners.items()
                if session_owner is owner
            )
        converged = True
        for session_key in session_keys:
            try:
                cls.close_session(session_key, owner=owner)
            except Exception as error:
                logger.warning(f"关闭任务浏览器失败，保留原线程重试：{error}")
                converged = False
        with cls._sessions_lock:
            closures = tuple(
                future for executor, future in cls._idle_closures.items()
                if cls._idle_close_owners.get(executor) is owner
            ) + tuple(
                future for closing_owner, future in cls._explicit_closures.values()
                if closing_owner is owner
            )
        for future in closures:
            try:
                future.result(timeout=_browser_close_budget())
            except Exception:
                converged = False
        converged = cls._retry_failed_sessions(owner=owner) and converged
        with cls._sessions_lock:
            return converged and not any(
                session.owner is owner
                for session in cls._sessions.values()
            ) and not any(
                session_owner is owner
                for session_owner in cls._session_executor_owners.values()
            )

    def with_session(
        self,
        session_key: str,
        callback: Callable[[_BrowserSessionState], Any],
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        timeout: Optional[int] = 30,
        owner: Optional[Any] = None,
    ) -> Any:
        """
        获取或创建浏览器会话，并在持有会话锁时执行回调。

        :param session_key: 会话标识
        :param callback: 使用浏览器会话执行操作的回调函数
        :param user_agent: 新建会话时使用的 User-Agent
        :param cookies: 本次操作要注入的 Cookie 请求头
        :param timeout: 默认操作超时时间，单位秒
        :param owner: 可选宿主作用域；提供时会隔离并校验会话归属
        :return: 回调函数返回值
        """
        # 会话线程只能执行自己的浏览器任务；不能在其中同步等待其他会话关闭。
        with self._sessions_lock:
            in_session_thread = threading.get_ident() in self._session_thread_ids.values()
        try:
            return self._run_in_session_thread(
                session_key,
                self._with_session_in_thread,
                session_key,
                callback,
                user_agent=user_agent,
                cookies=cookies,
                timeout=timeout,
                owner=owner,
            )
        finally:
            if not in_session_thread:
                self._enforce_session_limit(protect_session_key=session_key)

    @classmethod
    async def run_action_async(
        cls, session_key: str, callback: Callable[[], Any], owner: Optional[Any] = None,
    ) -> Any:
        """
        直接异步等待会话专属执行器，排队取实例时不占 Agent 公共工具线程。

        :param session_key: 动作所属的浏览器会话标识
        :param callback: 在该会话原线程执行的同步动作
        :param owner: 用于校验会话归属的宿主任务
        :return: 动作原始结果；取消只撤销当前调用的后续启动资格
        """
        cancelled = threading.Event()
        started = threading.Event()

        def invoke() -> Any:
            """
            标记已进入会话线程；等待队列中的调用取消时不影响当前使用者。
            """
            started.set()
            return callback()

        with browser_request(cancelled.is_set):
            executor, future = cls._submit_session_task(session_key, invoke, owner=owner)
            future.add_done_callback(partial(
                cls._finish_async_action, session_key, executor, cancelled, started, owner,
            ))
            try:
                async with asyncio.timeout(_browser_action_budget()):
                    result = await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                cancelled.set()
                if not started.is_set():
                    future.cancel()
                else:
                    cls._cancel_session_browser(session_key, owner)
                raise
            except TimeoutError as error:
                cancelled.set()
                if not started.is_set():
                    future.cancel()
                else:
                    cls._cancel_session_browser(session_key, owner)
                raise BrowserOperationTimeoutError("浏览器动作等待超时，实际执行结果未知") from error
        await asyncio.to_thread(cls._enforce_session_limit, session_key)
        return result

    @classmethod
    def _finish_async_action(
        cls, session_key: str, executor: ThreadPoolExecutor, cancelled: threading.Event,
        started: threading.Event, owner: Optional[Any], future: Future[Any],
    ) -> None:
        """
        观察被取消调用的真实终态，清理没有创建会话的空执行器。
        """
        if not future.cancelled():
            future.exception()
        if cancelled.is_set() and started.is_set():
            # 回调可能在取消检查后才发布实例；仍在原工作线程收口，补齐这个竞态。
            cls._cancel_session_browser(session_key, owner)
            try:
                cls._close_session_in_thread(session_key, owner=owner)
            except Exception as error:
                logger.warning(f"回收已中断的浏览器会话失败：{error}")
        with cls._sessions_lock:
            unused = (
                session_key not in cls._sessions
                and not cls._pending_operations.get(executor)
                and executor not in cls._failed_closures
            )
            if unused:
                cls._discard_session_executor(session_key, executor)
        if unused:
            executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _cancel_session_browser(cls, session_key: str, owner: Optional[Any]) -> None:
        """
        只撤销当前任务持有的原生实例，不因同名会话或等待者取消误伤其它任务。
        """
        with cls._sessions_lock:
            session = cls._sessions.get(session_key)
            browser_owner = (
                _context_browser_owner(session.context) if session and session.owner is owner else None
            )
        if browser_owner is not None:
            browser_owner.request_stop()

    def _with_session_in_thread(
        self,
        session_key: str,
        callback: Callable[[_BrowserSessionState], Any],
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        timeout: Optional[int] = 30,
        owner: Optional[Any] = None,
    ) -> Any:
        """
        在会话专属线程内获取浏览器会话并执行回调。

        :param session_key: 会话标识
        :param callback: 使用浏览器会话执行操作的回调函数
        :param user_agent: 新建会话时使用的 User-Agent
        :param cookies: 本次操作要注入的 Cookie 请求头
        :param timeout: 默认操作超时时间，单位秒
        :return: 回调函数返回值
        """
        session = self._get_or_create_session(
            session_key=session_key,
            user_agent=user_agent,
            cookies=cookies,
            owner=owner,
        )
        with session.lock:
            try:
                if timeout and hasattr(session.active_page, "set_default_timeout"):
                    session.active_page.set_default_timeout(int(timeout) * 1000)
                if cookies:
                    session.cookies = cookies
                    session.active_page.set_extra_http_headers({"cookie": cookies})
                return callback(session)
            finally:
                with self._sessions_lock:
                    session.last_used_at = time.monotonic()

    @classmethod
    def _run_in_session_thread(
        cls,
        session_key: str,
        callback: Callable[..., Any],
        *args: Any,
        owner: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """
        将浏览器同步 API 调用投递到当前会话固定的单线程执行器。

        :param session_key: 会话标识
        :param callback: 需要在会话线程中运行的回调
        :param args: 回调位置参数
        :param kwargs: 回调关键字参数
        :return: 回调返回值
        """
        if cls._is_current_session_thread(session_key):
            return callback(*args, owner=owner, **kwargs)

        for _ in range(2):
            try:
                executor, future = cls._submit_session_task(
                    session_key, partial(callback, *args, owner=owner, **kwargs), owner=owner,
                )
            except RuntimeError:
                continue
            try:
                return future.result(timeout=_browser_action_budget())
            except TimeoutError as error:
                cls._cancel_session_browser(session_key, owner)
                raise BrowserOperationTimeoutError("浏览器动作等待超时，实际执行结果未知") from error
            except Exception:
                with cls._sessions_lock:
                    has_session = session_key in cls._sessions
                if not has_session:
                    with cls._sessions_lock:
                        failed_close = executor in cls._failed_closures
                    if not failed_close and future.done():
                        cls._shutdown_session_executor(session_key, expected_executor=executor)
                raise

        raise RuntimeError("浏览器会话线程已关闭")

    @classmethod
    def _submit_session_task(
        cls, session_key: str, callback: Callable[..., Any], *args: Any,
        owner: Optional[Any] = None, **kwargs: Any,
    ) -> tuple[ThreadPoolExecutor, Future[Any]]:
        """
        原子提交带独立上下文的会话动作，并登记排队和在途计数。

        :param session_key: 会话标识
        :param callback: 在会话专属线程执行的同步回调
        :param args: 回调位置参数
        :param owner: 准入时校验的宿主任务
        :param kwargs: 回调关键字参数
        :return: 本次真实执行器和已登记的 Future
        """
        with cls._sessions_lock:
            executor = cls._get_session_executor(session_key, owner=owner)
            context = copy_context()

            def invoke_in_context() -> Any:
                """
                在请求快照中执行，避免后续动作继承先前请求的取消状态。
                """
                return context.run(cls._run_session_task, session_key, callback, *args, **kwargs)

            cls._pending_operations[executor] = cls._pending_operations.get(executor, 0) + 1
            try:
                future: Future[Any] = Context().run(executor.submit, invoke_in_context)
                future.add_done_callback(partial(cls._release_operation, executor))
            except RuntimeError:
                cls._release_operation(executor)
                cls._discard_session_executor(session_key, executor)
                raise
            return executor, future

    @classmethod
    def _release_operation(
        cls, executor: ThreadPoolExecutor, _future: Optional[Future[Any]] = None,
    ) -> None:
        """
        在任务终态或提交失败后释放计数，包含未执行就取消的排队任务。

        :param executor: 接受操作的执行器身份
        :param _future: 完成回调传入的终态句柄，提交失败时为空
        """
        with cls._sessions_lock:
            remaining = cls._pending_operations[executor] - 1
            if remaining:
                cls._pending_operations[executor] = remaining
            else:
                cls._pending_operations.pop(executor)

    @classmethod
    def _run_session_task(
        cls,
        session_key: str,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        记录当前会话工作线程后执行实际任务。

        :param session_key: 会话标识
        :param callback: 需要执行的回调
        :param args: 回调位置参数
        :param kwargs: 回调关键字参数
        :return: 回调返回值
        """
        with cls._sessions_lock:
            cls._session_thread_ids[session_key] = threading.get_ident()
        return callback(*args, **kwargs)

    @classmethod
    def _is_current_session_thread(cls, session_key: str) -> bool:
        """
        判断当前代码是否已运行在指定会话的固定线程内。

        :param session_key: 会话标识
        :return: 当前线程是会话工作线程时返回 True
        """
        with cls._sessions_lock:
            thread_id = cls._session_thread_ids.get(session_key)
        return thread_id == threading.get_ident()

    @classmethod
    def _get_session_executor(cls, session_key: str, owner: Optional[Any] = None) -> ThreadPoolExecutor:
        """
        获取或创建指定会话的单线程执行器。

        :param session_key: 会话标识
        :return: 会话专属执行器
        """
        with cls._sessions_lock:
            executor = cls._session_executors.get(session_key)
            if executor:
                existing_owner = cls._session_executor_owners.get(session_key)
                if existing_owner is not owner:
                    raise PermissionError("浏览器会话不属于当前任务")
                return executor
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=cls.SESSION_WORKER_NAME_PREFIX,
            )
            cls._session_executors[session_key] = executor
            cls._session_executor_owners[session_key] = owner
            return executor

    @classmethod
    def _get_existing_session_executor(
        cls, session_key: str, owner: Optional[Any] = None
    ) -> Optional[ThreadPoolExecutor]:
        """
        获取指定会话已存在的执行器。

        :param session_key: 会话标识
        :return: 已存在的执行器，不存在时返回 None
        """
        with cls._sessions_lock:
            executor = cls._session_executors.get(session_key)
            if executor and cls._session_executor_owners.get(session_key) is not owner:
                raise PermissionError("浏览器会话不属于当前任务")
            return executor

    @classmethod
    def _discard_session_executor(
        cls,
        session_key: str,
        executor: ThreadPoolExecutor,
    ) -> None:
        """
        丢弃已经关闭的会话执行器。

        :param session_key: 会话标识
        :param executor: 需要从缓存中移除的执行器
        """
        with cls._sessions_lock:
            if cls._session_executors.get(session_key) is executor:
                cls._session_executors.pop(session_key, None)
                cls._session_executor_owners.pop(session_key, None)
                cls._session_thread_ids.pop(session_key, None)

    @classmethod
    def _shutdown_session_executor(
        cls,
        session_key: str,
        wait: bool = True,
        expected_executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        """
        关闭并移除指定会话的执行器，不移除已经替换的同名执行器。

        :param session_key: 会话标识
        :param wait: 是否等待工作线程退出
        :param expected_executor: 调用方持有的执行器身份；None 表示取当前执行器
        """
        with cls._sessions_lock:
            executor = expected_executor or cls._session_executors.get(session_key)
            if executor:
                cls._discard_session_executor(session_key, executor)
        if executor:
            executor.shutdown(wait=wait, cancel_futures=True)

    @classmethod
    def _close_session_in_thread(cls, session_key: str, owner: Optional[Any] = None) -> bool:
        """
        在会话固定线程内关闭并移除浏览器会话。

        :param session_key: 会话标识
        :param owner: 可选宿主作用域；提供时校验会话归属
        :return: 找到并关闭会话时返回 True
        """
        completion: Future[bool] = Future()
        with cls._sessions_lock:
            session = cls._sessions.get(session_key)
            if session and session.owner is not owner:
                raise PermissionError("浏览器会话不属于当前任务")
            session = cls._sessions.pop(session_key, None)
            executor = cls._session_executors.get(session_key)
            if executor:
                cls._explicit_closures[executor] = (owner, completion)
                cls._discard_session_executor(session_key, executor)
        try:
            if session:
                cls._close_retired_session(session, executor)
            elif executor:
                executor.shutdown(wait=False, cancel_futures=True)
            completion.set_result(session is not None)
            return session is not None
        except BaseException as err:
            completion.set_exception(err)
            raise
        finally:
            with cls._sessions_lock:
                if executor:
                    cls._explicit_closures.pop(executor, None)

    def open_tab(
        self,
        session: _BrowserSessionState,
        url: Optional[str] = None,
        timeout: Optional[int] = 30,
        allow_private_network: bool = False,
    ) -> BrowserPage:
        """
        在当前会话中新建标签页，并可选导航到指定 URL。

        :param session: 当前浏览器会话
        :param url: 可选的目标 URL
        :param timeout: 导航超时时间，单位秒
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 新建的页面对象
        """
        page = session.context.new_page()
        if timeout and hasattr(page, "set_default_timeout"):
            page.set_default_timeout(int(timeout) * 1000)
        if session.cookies:
            page.set_extra_http_headers({"cookie": session.cookies})
        session.pages.append(page)
        session.active_index = len(session.pages) - 1
        if url:
            self.goto(
                page,
                url,
                timeout=timeout,
                allow_private_network=allow_private_network,
            )
        return page

    @staticmethod
    def list_tabs(session: _BrowserSessionState) -> list[dict[str, Any]]:
        """
        列出当前浏览器会话中的标签页。

        :param session: 当前浏览器会话
        :return: 标签页摘要列表
        """
        tabs = []
        for index, page in enumerate(session.pages):
            tabs.append(
                {
                    "index": index,
                    "active": index == session.active_index,
                    "url": getattr(page, "url", ""),
                    "title": BrowserSessionHelper._safe_page_title(page),
                }
            )
        return tabs

    @staticmethod
    def focus_tab(session: _BrowserSessionState, tab_index: int) -> BrowserPage:
        """
        切换当前会话的活动标签页。

        :param session: 当前浏览器会话
        :param tab_index: 标签页索引
        :return: 切换后的页面对象
        """
        if tab_index < 0 or tab_index >= len(session.pages):
            raise ValueError(f"标签页索引不存在: {tab_index}")
        session.active_index = tab_index
        return session.active_page

    @staticmethod
    def close_tab(session: _BrowserSessionState, tab_index: int) -> list[dict[str, Any]]:
        """
        关闭当前会话中的指定标签页。

        :param session: 当前浏览器会话
        :param tab_index: 标签页索引
        :return: 关闭后的标签页列表
        """
        if tab_index < 0 or tab_index >= len(session.pages):
            raise ValueError(f"标签页索引不存在: {tab_index}")
        active_index = session.active_index
        page = session.pages.pop(tab_index)
        close_error: Exception | None = None
        try:
            page.close()
        except Exception as err:
            close_error = err
            logger.warning(f"关闭浏览器标签页失败: {str(err)}")
        if not session.pages:
            session.pages.append(session.context.new_page())
        elif tab_index < active_index:
            active_index -= 1
        session.active_index = min(active_index, len(session.pages) - 1)
        if close_error is not None:
            raise RuntimeError("关闭浏览器标签页失败") from close_error
        return BrowserSessionHelper.list_tabs(session)

    @staticmethod
    def get_cookies(
        session: _BrowserSessionState,
        page: Optional[BrowserPage] = None,
    ) -> dict[str, Any]:
        """
        读取活动页面所属域名的 Cookie、User-Agent 和页面地址。
        """
        active_page = page or session.active_page
        current_url = getattr(active_page, "url", "") or ""
        current_host = (urlparse(current_url).hostname or "").lower().rstrip(".")
        values: dict[str, str] = {}
        if session.cookies:
            injected = cookie_parse(session.cookies)
            if isinstance(injected, dict):
                values.update({str(key): str(value) for key, value in injected.items()})

        for cookie in session.context.cookies() or []:
            if not isinstance(cookie, dict):
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            if name is None or value is None:
                continue
            domain = str(cookie.get("domain") or "").lower().lstrip(".").rstrip(".")
            if current_host and domain and current_host != domain and not current_host.endswith(f".{domain}"):
                continue
            values[str(name)] = str(value)

        user_agent = session.user_agent
        if not user_agent:
            try:
                user_agent = str(
                    active_page.evaluate("() => window.navigator.userAgent") or ""
                )
            except Exception:
                user_agent = ""
        cookie_header = "; ".join(f"{name}={value}" for name, value in values.items())
        return {
            "url": current_url,
            "user_agent": user_agent or "",
            "cookie": cookie_header,
            "cookies": [
                {"name": name, "value": value}
                for name, value in values.items()
            ],
        }

    def goto(
        self,
        page: BrowserPage,
        url: str,
        timeout: Optional[int] = 30,
        allow_private_network: bool = False,
    ) -> Any:
        """
        校验并导航页面到指定 URL。

        :param page: 页面对象
        :param url: 目标 URL
        :param timeout: 导航超时时间，单位秒
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 浏览器导航响应对象
        """
        self.validate_url(url, allow_private_network=allow_private_network)
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(timeout or 30) * 1000,
        )
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=min(int(timeout or 30), 15) * 1000,
            )
        except Exception:
            pass
        self.validate_current_url(page, allow_private_network=allow_private_network)
        return response

    @classmethod
    def validate_current_url(
        cls, page: BrowserPage, allow_private_network: bool = False
    ) -> None:
        """
        校验当前页面地址，捕获跳转后的不安全目标。

        :param page: 页面对象
        :param allow_private_network: 是否允许访问本机或私网地址
        """
        current_url = getattr(page, "url", "")
        if current_url and current_url.startswith(("http://", "https://")):
            cls.validate_url(current_url, allow_private_network=allow_private_network)

    @classmethod
    def build_snapshot(
        cls,
        page: BrowserPage,
        status: Optional[Any] = None,
        max_text_chars: int = 8000,
        max_elements: int = 40,
    ) -> dict[str, Any]:
        """
        构建包含可读文本和可交互元素 ref 的页面快照。

        :param page: 页面对象
        :param status: 可选的导航状态码
        :param max_text_chars: 页面文本最大返回长度
        :param max_elements: 最大可交互元素数量
        :return: 页面快照字典
        """
        text_content = cls._safe_inner_text(page, "body")
        elements = cls._extract_interactive_elements(page, max_elements=max_elements)
        result: dict[str, Any] = {
            "url": getattr(page, "url", ""),
            "title": cls._safe_page_title(page),
            "text_content": cls._truncate_text(text_content, max_text_chars),
            "interactive_elements": elements,
        }
        if status is not None:
            result["status"] = status

        links = [
            {
                "ref": element.get("ref"),
                "text": element.get("text"),
                "href": element.get("href"),
            }
            for element in elements
            if element.get("tag") == "a" and element.get("href")
        ][:30]
        forms = [
            element
            for element in elements
            if element.get("tag") in {"input", "textarea", "select", "button"}
        ][:30]
        if links:
            result["links"] = links
        if forms:
            result["form_elements"] = forms
        return result

    @staticmethod
    def _launch_context(
        headless: bool,
        user_agent: Optional[str] = None,
        viewport: Optional[dict[str, int]] = None,
    ) -> BrowserContext:
        """
        按宿主反检测配置创建 CloakBrowser 上下文。
        """
        context_kwargs = {
            "humanize": get_runtime_setting('CLOAKBROWSER_HUMANIZE'),
            "human_preset": get_runtime_setting('CLOAKBROWSER_HUMAN_PRESET'),
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if viewport:
            context_kwargs["viewport"] = viewport
        return launch_browser_context(headless=headless, **context_kwargs)

    def _get_or_create_session(
        self,
        session_key: str,
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        owner: Optional[Any] = None,
    ) -> _BrowserSessionState:
        """
        复用匹配会话，或为新的会话键创建上下文。
        """
        self._validate_owner(owner)
        retired_session = None
        with self._sessions_lock:
            session = self._sessions.get(session_key)
            if session and session.owner is not owner:
                raise PermissionError("浏览器会话不属于当前任务")
            browser_owner = _context_browser_owner(session.context) if session else None
            if session and (
                (user_agent and session.user_agent != user_agent)
                or (browser_owner is not None and browser_owner.expired)
            ):
                retired_session = self._sessions.pop(session_key)
                session = None
            if session:
                return session
        if retired_session is not None:
            try:
                self._close_session_state(retired_session)
            except Exception:
                with self._sessions_lock:
                    self._sessions[session_key] = retired_session
                raise

        with browser_request(lambda: owner is not None and bool(getattr(owner, "closed", False))):
            context = self._launch_context(
                headless=self.headless,
                user_agent=user_agent,
                viewport=self.viewport,
            )
            page = self._create_session_page(context, session_key, cookies, owner)
        session = _BrowserSessionState(
            session_key=session_key,
            context=context,
            pages=[page],
            user_agent=user_agent,
            cookies=cookies,
            owner=owner,
        )
        with self._sessions_lock:
            existing_session = self._sessions.get(session_key)
            if existing_session is None:
                self._sessions[session_key] = session
                return session
        self._close_session_state(session)
        if existing_session.owner is not owner:
            raise PermissionError("浏览器会话不属于当前任务")
        return existing_session

    @classmethod
    def _create_session_page(
        cls, context: BrowserContext, session_key: str, cookies: Optional[str], owner: Optional[Any],
    ) -> BrowserPage:
        """
        初始化首个页面；失败后仍无法关闭实例时保留原线程，以便任务收口重试。

        :param context: 已经取得名额并启动的上下文
        :param session_key: 新会话的索引标识
        :param cookies: 可选的初始 Cookie 请求头
        :param owner: 持有本次实例的宿主任务
        :return: 完成初始化的第一个页面
        """
        try:
            check_browser_request()
            page = context.new_page()
            if cookies:
                page.set_extra_http_headers({"cookie": cookies})
            return page
        except BaseException:
            try:
                context.close()
            except Exception:
                with cls._sessions_lock:
                    executor = cls._session_executors.get(session_key)
                    if executor is not None:
                        cls._failed_closures[executor] = _BrowserSessionState(
                            session_key=session_key, context=context, pages=[], owner=owner,
                        )
                        cls._discard_session_executor(session_key, executor)
                raise
            raise

    @staticmethod
    def _validate_owner(owner: Optional[Any]) -> None:
        """
        拒绝已封口作用域创建或复用浏览器会话。
        """
        if owner is not None and bool(getattr(owner, "closed", False)):
            raise PermissionError("浏览器会话所属任务已停止")

    @classmethod
    def _prune_sessions(cls) -> None:
        """
        向已有会话线程投递条件关闭，不等待浏览器 I/O，不创建新的执行器。
        """
        with cls._sessions_lock:
            for session_key, session in tuple(cls._sessions.items()):
                executor = cls._session_executors.get(session_key)
                if (
                    executor is None
                    or executor in cls._idle_closures
                    or cls._pending_operations.get(executor, 0)
                    or time.monotonic() - session.last_used_at <= cls.SESSION_TTL_SECONDS
                ):
                    continue
                try:
                    future = Context().run(
                        executor.submit, cls._close_idle_session,
                        session_key, session, executor, session.owner,
                    )
                except RuntimeError as err:
                    logger.warning(f"投递空闲浏览器关闭失败：{err}")
                    continue
                cls._idle_closures[executor] = future
                cls._idle_close_owners[executor] = session.owner
                future.add_done_callback(cls._idle_close_done)

    @classmethod
    def _close_idle_session(
        cls,
        session_key: str,
        session: _BrowserSessionState,
        executor: ThreadPoolExecutor,
        owner: Optional[Any],
    ) -> bool:
        """
        在原线程复核候选，并原子撤销旧会话和执行器的准入后关闭浏览器。

        :param session_key: 扫描时的会话标识
        :param session: 扫描时的会话实例，替换后的实例不能被旧任务关闭
        :param executor: 扫描时持有该会话的执行器
        :param owner: 扫描时的宿主作用域身份
        :return: 候选仍空闲且已关闭时返回 True
        """
        with cls._sessions_lock:
            if (
                cls._sessions.get(session_key) is not session
                or cls._session_executors.get(session_key) is not executor
                or cls._session_executor_owners.get(session_key) is not owner
                or session.owner is not owner
                or cls._pending_operations.get(executor, 0)
                or time.monotonic() - session.last_used_at <= cls.SESSION_TTL_SECONDS
            ):
                return False
            cls._sessions.pop(session_key)
            cls._discard_session_executor(session_key, executor)
        # 新操作只能创建新的上下文；旧关闭不能移除同名的新执行器。
        cls._close_retired_session(session, executor)
        return True

    @classmethod
    def _idle_close_done(cls, future: Future[bool]) -> None:
        """
        观察关闭结果并释放已终止的回收句柄，防止重复投递或静默丢失异常。
        """
        with cls._sessions_lock:
            for executor, current in tuple(cls._idle_closures.items()):
                if current is future:
                    cls._idle_closures.pop(executor)
                    cls._idle_close_owners.pop(executor, None)
        if not future.cancelled() and future.exception() is not None:
            logger.warning(f"关闭空闲浏览器失败：{future.exception()}")

    @classmethod
    def _enforce_session_limit(cls, protect_session_key: Optional[str] = None) -> None:
        """
        清理超过数量上限的旧会话。

        :param protect_session_key: 本次刚创建、需要优先保留的会话标识
        """
        while True:
            with cls._sessions_lock:
                if len(cls._sessions) <= cls.MAX_SESSIONS:
                    return
                candidate_keys = [
                    session_key
                    for session_key in cls._sessions
                    if session_key != protect_session_key
                ]
                if not candidate_keys:
                    return
                oldest_key = min(
                    candidate_keys,
                    key=lambda key: cls._sessions[key].last_used_at,
                )
                oldest_owner = cls._sessions[oldest_key].owner
            if not cls.close_session(oldest_key, owner=oldest_owner):
                return

    @staticmethod
    def _close_session_state(session: _BrowserSessionState) -> None:
        """
        逐个关闭页面并尝试关闭实例；实例关闭失败向生命周期 owner 报告。
        """
        with session.lock:
            browser_owner = _context_browser_owner(session.context)
            if browser_owner is not None:
                browser_owner.begin_close()
            for page in list(session.pages):
                try:
                    page.close()
                except Exception as err:
                    logger.warning(f"关闭浏览器页面失败: {str(err)}")
            try:
                session.context.close()
            except Exception as err:
                logger.warning(f"关闭浏览器上下文失败: {str(err)}")
                raise

    @classmethod
    def _close_retired_session(
        cls, session: _BrowserSessionState, executor: Optional[ThreadPoolExecutor],
    ) -> None:
        """
        在原线程收敛已撤销准入的实例，失败时保留句柄和可重试执行器。

        :param session: 需要终止的旧会话
        :param executor: 该会话的原工作线程；无执行器时由当前调用者继续负责
        """
        try:
            cls._close_session_state(session)
        except BaseException:
            if executor is not None:
                with cls._sessions_lock:
                    cls._failed_closures[executor] = session
            raise
        if executor is not None:
            with cls._sessions_lock:
                cls._failed_closures.pop(executor, None)
            executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _retry_failed_sessions(
        cls, owner: Optional[Any] = None, session_key: Optional[str] = None, all_owners: bool = False,
    ) -> bool:
        """
        在关闭失败的原执行器上重试，不重建线程或假报已经释放名额。

        :param owner: 只重试该任务持有的旧实例
        :param session_key: 可选的单个会话标识
        :param all_owners: 全局关闭时忽略任务筛选
        :return: 所有选中实例均已成功关闭时返回 True
        """
        with cls._sessions_lock:
            failed = tuple(
                (executor, session) for executor, session in cls._failed_closures.items()
                if (all_owners or session.owner is owner)
                and (session_key is None or session.session_key == session_key)
            )
        converged = True
        for executor, session in failed:
            try:
                future = Context().run(executor.submit, cls._close_retired_session, session, executor)
                future.result(timeout=_browser_close_budget())
            except Exception as error:
                logger.warning(f"重试关闭浏览器失败：{error}")
                converged = False
        return converged

    @staticmethod
    def _safe_page_title(page: BrowserPage) -> str:
        """
        读取页面标题，页面失效时返回空字符串。
        """
        try:
            return page.title()
        except Exception:
            return ""

    @staticmethod
    def _safe_inner_text(page: BrowserPage, selector: str) -> str:
        """
        读取节点文本，选择器或页面失效时返回空字符串。
        """
        try:
            return page.inner_text(selector)
        except Exception:
            return ""

    @staticmethod
    def _truncate_text(text: Optional[str], max_chars: int) -> str:
        """
        按 Agent 上下文上限截断页面文本。
        """
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n...(内容已截断)"

    @classmethod
    def _extract_interactive_elements(
        cls, page: BrowserPage, max_elements: int
    ) -> list[dict[str, Any]]:
        """
        提取页面中可见且可交互的元素快照。
        """
        script = f"""
            () => {{
                const limit = {int(max_elements)};
                document.querySelectorAll('[{cls.REF_ATTRIBUTE}]').forEach((el) =>
                    el.removeAttribute('{cls.REF_ATTRIBUTE}'));
                const selector = [
                    'a[href]',
                    'button',
                    'input',
                    'textarea',
                    'select',
                    '[role="button"]',
                    '[role="link"]',
                    '[onclick]',
                    'summary'
                ].join(',');
                const isVisible = (el) => {{
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                }};
                return Array.from(document.querySelectorAll(selector))
                    .filter(isVisible)
                    .slice(0, limit)
                    .map((el, index) => {{
                        const ref = `e${{index + 1}}`;
                        el.setAttribute('{cls.REF_ATTRIBUTE}', ref);
                        const tag = el.tagName.toLowerCase();
                        const text = (
                            el.innerText
                            || el.value
                            || el.getAttribute('aria-label')
                            || el.getAttribute('title')
                            || el.getAttribute('placeholder')
                            || ''
                        ).trim();
                        return {{
                            ref,
                            tag,
                            type: el.type || '',
                            text: text.substring(0, 120),
                            name: el.name || '',
                            id: el.id || '',
                            role: el.getAttribute('role') || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            href: el.href || '',
                            value: tag === 'select' ? '' : (el.value || '').substring(0, 80),
                            selector: `[${cls.REF_ATTRIBUTE}="${{ref}}"]`
                        }};
                    }});
            }}
        """
        try:
            elements = page.evaluate(script)
        except Exception as err:
            logger.debug(f"提取页面可交互元素失败: {str(err)}")
            return []
        if not isinstance(elements, list):
            return []
        return elements


class PlaywrightHelper:
    """
    为旧插件浏览器调用提供统一的 CloakBrowser 适配。
    """

    def __init__(self, browser_type: Optional[str] = None, *args: Any, **kwargs: Any) -> None:
        """
        兼容旧的 PlaywrightHelper(browser_type=...) 构造方式。
        """
        self.browser_type = browser_type or get_runtime_setting(
            "PLAYWRIGHT_BROWSER_TYPE"
        )

    @staticmethod
    def __browser_emulation() -> str:
        """
        当前浏览器仿真类型。
        """
        return (
            get_runtime_setting('BROWSER_EMULATION') or "cloakbrowser"
        ).lower()

    @staticmethod
    def __launch_cloakbrowser_context(headless: bool,
                                      user_agent: Optional[str] = None,
                                      proxies: Optional[dict[str, Any]] = None) -> BrowserContext:
        """
        启动 CloakBrowser 上下文。
        """
        return launch_browser_context(headless=headless,
                                      proxy=proxies,
                                      user_agent=user_agent,
                                      humanize=get_runtime_setting('CLOAKBROWSER_HUMANIZE'),
                                      human_preset=get_runtime_setting('CLOAKBROWSER_HUMAN_PRESET'))

    @staticmethod
    def __fs_cookie_str(cookies: list[dict[str, Any]]) -> str:
        """
        将 FlareSolverr Cookie 列表转换为请求头字符串。
        """
        if not cookies:
            return ""
        return "; ".join([f"{c.get('name')}={c.get('value')}" for c in cookies if c and c.get('name') is not None])

    @staticmethod
    def __flaresolverr_request(url: str,
                               cookies: Optional[str] = None,
                               proxy_config: Optional[dict[str, Any]] = None,
                               timeout: Optional[int] = 60) -> Optional[dict[str, Any]]:
        """
        调用 FlareSolverr 解决 Cloudflare 并返回 solution 结果
        参考: https://github.com/FlareSolverr/FlareSolverr
        """
        flaresolverr_url = get_runtime_setting('FLARESOLVERR_URL')
        if not flaresolverr_url:
            logger.warn("未配置 FLARESOLVERR_URL，无法使用 FlareSolverr")
            return None

        fs_api = flaresolverr_url.rstrip("/") + "/v1"
        session_id = None

        try:
            # 检查是否需要代理认证
            need_proxy_auth = (proxy_config and proxy_config.get("server") and
                               (proxy_config.get("username") or proxy_config.get("password")))

            if need_proxy_auth:
                # 使用 session 模式支持代理认证
                logger.debug("检测到flaresolverr代理需要认证，使用 session 模式")

                session_id = str(uuid.uuid4())
                create_payload: dict[str, Any] = {
                    "cmd": "sessions.create",
                    "session": session_id
                }

                # 添加代理配置到会话创建请求
                if proxy_config and proxy_config.get("server"):
                    proxy_payload: dict[str, Any] = {"url": proxy_config["server"]}
                    if proxy_config.get("username"):
                        proxy_payload["username"] = proxy_config["username"]
                    if proxy_config.get("password"):
                        proxy_payload["password"] = proxy_config["password"]
                    create_payload["proxy"] = proxy_payload

                create_result = RequestUtils(content_type="application/json",
                                             timeout=timeout or 60).post_json(url=fs_api, json=create_payload)
                if not create_result or create_result.get("status") != "ok":
                    logger.error(
                        f"创建 FlareSolverr 会话失败: {create_result.get('message') if create_result else '无响应'}")
                    return None

                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "session": session_id,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
            else:
                # 使用普通模式（无代理认证）
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
                # 添加代理配置（仅 URL，无认证）
                if proxy_config and proxy_config.get("server"):
                    request_payload["proxy"] = {"url": proxy_config["server"]}

            # 将 cookies 以数组形式传递给 FlareSolverr
            if cookies:
                try:
                    request_payload["cookies"] = cookie_parse(cookies, array=True)
                except Exception as e:
                    logger.debug(f"解析 cookies 失败，忽略: {str(e)}")

            data = RequestUtils(content_type="application/json",
                                timeout=timeout or 60).post_json(url=fs_api, json=request_payload)
            if not data:
                logger.error("FlareSolverr 返回空响应")
                return None
            if data.get("status") != "ok":
                logger.error(f"FlareSolverr 调用失败: {data.get('message')}")
                return None
            return data.get("solution")
        except Exception as e:
            logger.error(f"调用 FlareSolverr 失败: {str(e)}")
            return None
        finally:
            if session_id:
                try:
                    destroy_payload = {
                        "cmd": "sessions.destroy",
                        "session": session_id
                    }
                    RequestUtils(content_type="application/json",
                                 timeout=10).post_json(url=fs_api, json=destroy_payload)
                    logger.debug(f"已清理 FlareSolverr 会话: {session_id}")
                except Exception as e:
                    logger.warning(f"清理 FlareSolverr 会话失败: {str(e)}")

    def action(self, url: str,
               callback: Callable[[BrowserPage], Any],
               cookies: Optional[str] = None,
               ua: Optional[str] = None,
               proxies: Optional[dict[str, Any]] = None,
               headless: Optional[bool] = False,
               timeout: Optional[int] = 60) -> Any:
        """
        访问页面并在受控浏览器会话内执行回调，结束后关闭页面与上下文。

        配置 FlareSolverr 时优先使用其返回的 Cookie 和浏览器标识，回调不可把页面留到会话外使用。
        异常会记录日志；关闭异常不会覆盖已经获得的回调结果。

        :param url: 要访问的页面地址
        :param callback: 接收页面对象并返回业务结果的同步回调
        :param cookies: 初始 Cookie 请求头文本；可被 FlareSolverr 结果替代
        :param ua: 浏览器标识；可被 FlareSolverr 结果替代
        :param proxies: 浏览器代理配置；None 表示未指定
        :param headless: 是否使用无头模式，默认使用有界面模式
        :param timeout: 导航超时，单位为秒；None 或 0 回退到 60 秒
        :return: 回调结果；初始化、导航或回调失败而未取得结果时返回 None
        """
        timeout = timeout or 60
        result = None
        try:
            context = None
            page = None
            try:
                # 如果配置使用 FlareSolverr，先通过其获取清除后的 cookies 与 UA
                fs_cookie_header = None
                fs_ua = None
                if self.__browser_emulation() == "flaresolverr":
                    solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                           proxy_config=proxies, timeout=timeout)
                    if solution:
                        fs_cookie_header = self.__fs_cookie_str(solution.get("cookies", []))
                        fs_ua = solution.get("userAgent")

                context = self.__launch_cloakbrowser_context(headless=bool(headless),
                                                             user_agent=fs_ua or ua,
                                                             proxies=proxies)
                page = context.new_page()

                # 优先使用 FlareSolverr 返回，其次使用入参
                merged_cookie = fs_cookie_header or cookies
                if merged_cookie:
                    page.set_extra_http_headers({"cookie": merged_cookie})

                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                try:
                    # 登录页的统计与长连接请求可能持续存在，不应阻断已就绪表单的处理。
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=min(timeout, 15) * 1000,
                    )
                except Exception:
                    pass

                result = callback(page)

            except Exception as e:
                logger.error(f"网页操作失败: {str(e)}")
            finally:
                try:
                    if page:
                        page.close()
                finally:
                    if context:
                        context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return result

    def get_page_source(self, url: str,
                        cookies: Optional[str] = None,
                        ua: Optional[str] = None,
                        proxies: Optional[dict[str, Any]] = None,
                        headless: Optional[bool] = False,
                        timeout: Optional[int] = 60) -> Optional[str]:
        """
        获取网页源码，优先尝试配置的 FlareSolverr，再回退到浏览器读取。

        浏览器路径会等待挑战页标题消失或达到期限，读取结束后释放页面和上下文。

        :param url: 要读取的页面地址
        :param cookies: 发送给页面的 Cookie 请求头文本
        :param ua: 指定的浏览器标识，未指定时使用浏览器默认值
        :param proxies: 浏览器代理配置；None 表示未指定
        :param headless: 是否使用无头模式，默认使用有界面模式
        :param timeout: 页面加载与挑战等待上限，单位为秒；None 或 0 回退到 60 秒
        :return: 获取到的 HTML 文本；未取得源码时返回 None
        """
        timeout = timeout or 60
        source = None
        # 如果配置为 FlareSolverr，则直接调用获取页面源码
        if self.__browser_emulation() == "flaresolverr":
            try:
                solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                       proxy_config=proxies, timeout=timeout)
                if solution:
                    return solution.get("response")
            except Exception as e:
                logger.error(f"FlareSolverr 获取源码失败: {str(e)}")
        try:
            context = None
            page = None
            try:
                context = self.__launch_cloakbrowser_context(headless=bool(headless),
                                                             user_agent=ua,
                                                             proxies=proxies)
                page = context.new_page()

                if cookies:
                    page.set_extra_http_headers({"cookie": cookies})

                page.goto(url, wait_until="load", timeout=timeout * 1000)

                # 部分挑战页会持续轮询，无法进入 networkidle；加载后按标题判断是否仍需等待。
                challenge_titles = ("just a moment", "请稍候", "loading")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        current_title = (page.title() or "").strip().lower()
                    except Exception:
                        current_title = ""
                    if current_title and not any(
                            t in current_title for t in challenge_titles):
                        break
                    time.sleep(2)

                # 页面跳转中 content() 可能失败, 重试几次
                source = None
                for _attempt in range(5):
                    try:
                        source = page.content()
                        if source:
                            break
                    except Exception:
                        source = None
                        time.sleep(2)

            except Exception as e:
                logger.error(f"获取网页源码失败: {str(e)}")
                source = None
            finally:
                # 页面关闭失败也必须尝试终止整个实例，避免一直占用唯一名额。
                try:
                    if page:
                        page.close()
                finally:
                    if context:
                        context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return source
