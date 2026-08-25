import socket
import threading
import time

from app.adapters.network import doh
from app.runtime.correlation import correlation_scope, get_correlation_id
from app.runtime.execution import OwnedThreadPoolExecutor


def test_doh_executor_is_lazy_and_shutdown_restores_socket(monkeypatch):
    """DoH 线程池按需创建，并在模块关闭时恢复系统 DNS"""
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: "203.0.113.7")
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    try:
        assert helper.shutdown() is True
        assert doh._executor is None

        assert doh.enable_doh(True) is True
        socket.getaddrinfo("example.com", None)
        executor = doh._executor
        assert isinstance(executor, OwnedThreadPoolExecutor)

        assert helper.shutdown() is True

        assert doh._executor is None
        assert socket.getaddrinfo is doh._orig_getaddrinfo
        assert getattr(executor, "_shutdown", False)
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo


def test_doh_shutdown_is_bounded_and_retryable(monkeypatch):
    """阻塞查询超时时保留同一 owner，释放后可重试并安全重新启用。"""
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    entered = threading.Event()
    release = threading.Event()
    future = None
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    def blocked_query() -> None:
        """模拟底层网络栈未按 DoH 请求超时返回的同步查询。"""
        entered.set()
        release.wait()

    try:
        assert helper.shutdown(timeout=1) is True
        assert doh.enable_doh(True) is True
        with doh._executor_lock:
            executor = doh._get_executor_locked()
        future = executor.submit(blocked_query)
        assert entered.wait(timeout=1)

        started_at = time.monotonic()
        assert helper.shutdown(timeout=0.01) is False
        assert time.monotonic() - started_at < 1
        assert doh._executor is executor
        assert socket.getaddrinfo is doh._orig_getaddrinfo
        assert executor.accepting is False

        # 未收敛 owner 不得被新 executor 覆盖，否则旧查询会脱离生命周期追踪。
        assert doh.enable_doh(True) is False
        assert doh._executor is executor

        release.set()
        future.result(timeout=1)
        assert helper.shutdown(timeout=1) is True
        assert doh._executor is None
        assert doh.enable_doh(True) is True
    finally:
        release.set()
        if future is not None:
            future.result(timeout=1)
        helper.shutdown(timeout=1)
        socket.getaddrinfo = original_getaddrinfo


def test_doh_config_reload_disables_and_closes_executor(monkeypatch):
    """热更新关闭 DoH 时恢复系统 DNS 并释放已创建的线程池"""
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: "203.0.113.7")
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        socket.getaddrinfo("example.com", None)
        executor = doh._executor
        assert executor is not None
        monkeypatch.setattr("app.runtime.config.settings.DOH_ENABLE", False)

        helper.on_config_changed()

        assert doh._executor is None
        assert getattr(executor, "_shutdown", False)
        assert socket.getaddrinfo is doh._orig_getaddrinfo
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo


def test_enable_doh_reuses_cached_host_resolution(monkeypatch):
    """
    同一 DoH 域名第二次解析应命中缓存，避免重复请求远端解析器。
    """
    query_calls = []
    resolved_hosts = []

    def fake_query(resolver: str, host: str) -> str:
        query_calls.append((resolver, host))
        return "203.0.113.7"

    def fake_getaddrinfo(host: str, *args, **kwargs):
        resolved_hosts.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))]

    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", fake_query)
    monkeypatch.setattr(doh, "_orig_getaddrinfo", fake_getaddrinfo)

    original_getaddrinfo = socket.getaddrinfo
    with doh._doh_lock:
        doh._doh_cache.clear()

    try:
        assert doh.enable_doh(True) is True

        socket.getaddrinfo("example.com", None)
        socket.getaddrinfo("example.com", None)
    finally:
        object.__new__(doh.DohHelper).shutdown()
        socket.getaddrinfo = original_getaddrinfo
        with doh._doh_lock:
            doh._doh_cache.clear()

    assert query_calls == [("resolver.test", "example.com")]
    assert resolved_hosts == ["203.0.113.7", "203.0.113.7"]


def test_doh_queries_use_each_request_context(monkeypatch):
    """复用的 DoH worker 应按查询恢复关联 ID，不能丢失或粘住首个请求。"""
    original_getaddrinfo = socket.getaddrinfo
    observed = []
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr(
        "app.runtime.config.settings.DOH_DOMAINS",
        "first.example,second.example",
    )
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(
        doh,
        "_doh_query",
        lambda _resolver, _host: observed.append(get_correlation_id()) or "203.0.113.7",
    )
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda _host, *_args, **_kwargs: [])

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        with correlation_scope("doh-first"):
            socket.getaddrinfo("first.example", None)
        with correlation_scope("doh-second"):
            socket.getaddrinfo("second.example", None)
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo

    assert observed == ["doh-first", "doh-second"]
