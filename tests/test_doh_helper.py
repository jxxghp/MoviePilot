import socket

from app.helper import doh


def test_doh_executor_is_lazy_and_shutdown_restores_socket(monkeypatch):
    """DoH 线程池按需创建，并在模块关闭时恢复系统 DNS"""
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr(doh.settings, "DOH_DOMAINS", "example.com")
    monkeypatch.setattr(doh.settings, "DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: "203.0.113.7")
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    try:
        helper.shutdown()
        assert doh._executor is None

        doh.enable_doh(True)
        socket.getaddrinfo("example.com", None)
        executor = doh._executor
        assert executor is not None

        helper.shutdown()

        assert doh._executor is None
        assert socket.getaddrinfo is doh._orig_getaddrinfo
        assert getattr(executor, "_shutdown", False)
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo


def test_doh_config_reload_disables_and_closes_executor(monkeypatch):
    """热更新关闭 DoH 时恢复系统 DNS 并释放已创建的线程池"""
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr(doh.settings, "DOH_DOMAINS", "example.com")
    monkeypatch.setattr(doh.settings, "DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: "203.0.113.7")
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    try:
        helper.shutdown()
        doh.enable_doh(True)
        socket.getaddrinfo("example.com", None)
        executor = doh._executor
        assert executor is not None
        monkeypatch.setattr(doh.settings, "DOH_ENABLE", False)

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

    monkeypatch.setattr(doh.settings, "DOH_DOMAINS", "example.com")
    monkeypatch.setattr(doh.settings, "DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", fake_query)
    monkeypatch.setattr(doh, "_orig_getaddrinfo", fake_getaddrinfo)

    original_getaddrinfo = socket.getaddrinfo
    with doh._doh_lock:
        doh._doh_cache.clear()

    try:
        doh.enable_doh(True)

        socket.getaddrinfo("example.com", None)
        socket.getaddrinfo("example.com", None)
    finally:
        object.__new__(doh.DohHelper).shutdown()
        socket.getaddrinfo = original_getaddrinfo
        with doh._doh_lock:
            doh._doh_cache.clear()

    assert query_calls == [("resolver.test", "example.com")]
    assert resolved_hosts == ["203.0.113.7", "203.0.113.7"]
