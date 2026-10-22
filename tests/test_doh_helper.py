"""
验证加密 DNS 执行器的惰性创建、关闭和套接字恢复。
"""

import json
import socket
import struct
import threading
import time
import urllib.request

import pytest

from app.adapters.network import doh
from app.runtime.correlation import correlation_scope, get_correlation_id
from app.runtime.execution import OwnedThreadPoolExecutor


def test_doh_executor_is_lazy_and_shutdown_restores_socket(monkeypatch):
    """
    DoH 线程池按需创建，并在模块关闭时恢复系统 DNS
    """
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
    """
    阻塞查询超时时保留同一 owner，释放后可重试并安全重新启用。
    """
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    entered = threading.Event()
    release = threading.Event()
    future = None
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])

    def blocked_query() -> None:
        """
        模拟底层网络栈未按 DoH 请求超时返回的同步查询。
        """
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
    """
    热更新关闭 DoH 时恢复系统 DNS 并释放已创建的线程池
    """
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
    """
    复用的 DoH worker 应按查询恢复关联 ID，不能丢失或粘住首个请求。
    """
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


def _dns_name(name: str) -> bytes:
    """
    按 RFC 1035 编码未压缩的域名。
    """
    return b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\x00"


def _dns_response(host: str, answers: list[tuple[bytes, int, bytes]], rcode: int = 0) -> bytes:
    """
    构造单问题 DNS 应答报文。

    :param host: 问题区的域名
    :param answers: 依次排列的应答记录，每项为 (编码后的记录名, 记录类型, rdata)
    :param rcode: 响应码，0 表示成功
    :return: 完整报文
    """
    header = struct.pack(">HHHHHH", 0, 0x8180 | rcode, 1, len(answers), 0, 0)
    question = _dns_name(host) + struct.pack(">HH", 1, 1)
    records = b"".join(
        name + struct.pack(">HHIH", record_type, 1, 60, len(rdata)) + rdata
        for name, record_type, rdata in answers
    )
    return header + question + records


def _cname_then_a_response() -> bytes:
    """
    构造 image.tmdb.org 先返回 CNAME、再返回 CDN A 记录的真实形态应答。

    第一条记录名压缩指向问题区，第二条记录名压缩指向 CNAME 的目标域名。
    """
    host = "image.tmdb.org"
    question_end = 12 + len(_dns_name(host)) + 4
    cname_rdata_offset = question_end + 2 + 10
    return _dns_response(
        host,
        [
            (b"\xc0\x0c", 5, _dns_name("tmdb-image-prod.b-cdn.net")),
            (struct.pack(">H", 0xC000 | cname_rdata_offset), 1, socket.inet_aton("169.150.249.164")),
        ],
    )


class _FakeDohResponse:
    """
    模拟 urllib 返回的 DoH 响应上下文。
    """

    def __init__(self, body: bytes, status: int = 200) -> None:
        """
        :param body: 响应正文
        :param status: HTTP 状态码
        """
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeDohResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        """
        返回完整响应正文。
        """
        return self._body


def test_doh_query_follows_cname_to_a_record(monkeypatch):
    """
    应答先给 CNAME 时应跳过别名记录取 A 记录，而不是把别名字节当成 IP。
    """
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeDohResponse(_cname_then_a_response()),
    )

    # 旧实现按固定偏移读取，会把 "\x0ftmd" 误解析为 15.116.109.100
    assert doh._doh_query("resolver.test", "image.tmdb.org") == "169.150.249.164"


@pytest.mark.parametrize(
    "message",
    [
        _dns_response("example.com", [], rcode=3),
        _dns_response("example.com", [(b"\xc0\x0c", 5, _dns_name("alias.example.net"))]),
        _dns_response("example.com", [(b"\xc0\x0c", 1, socket.inet_aton("203.0.113.7"))])[:-2],
        _dns_response("example.com", [(b"\x40", 1, socket.inet_aton("203.0.113.7"))]),
        b"\x00\x00\x81\x80",
    ],
    ids=["nxdomain", "cname-only", "truncated-rdata", "reserved-label", "short-header"],
)
def test_parse_dns_a_record_rejects_unusable_answers(message):
    """
    失败响应码、没有 A 记录、截断报文和保留标签类型都不应产生地址。
    """
    assert doh._parse_dns_a_record(message) is None


def test_parse_dns_a_record_reads_plain_a_record():
    """
    直接返回 A 记录的应答应取第一条地址。
    """
    message = _dns_response(
        "example.com",
        [
            (b"\xc0\x0c", 1, socket.inet_aton("203.0.113.7")),
            (b"\xc0\x0c", 1, socket.inet_aton("203.0.113.8")),
        ],
    )

    assert doh._parse_dns_a_record(message) == "203.0.113.7"


def test_doh_query_json_skips_cname(monkeypatch):
    """
    JSON 应答同样应跳过 CNAME 链，只返回 A 记录地址。
    """
    body = json.dumps({
        "Status": 0,
        "Answer": [
            {"name": "image.tmdb.org.", "type": 5, "data": "tmdb-image-prod.b-cdn.net."},
            {"name": "tmdb-image-prod.b-cdn.net.", "type": 1, "data": "169.150.249.164"},
        ],
    }).encode()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeDohResponse(body),
    )

    assert doh.doh_query_json("resolver.test", "image.tmdb.org") == "169.150.249.164"


def test_doh_query_logs_resolver_failure_at_debug(monkeypatch):
    """
    单个解析器超时只记调试日志，不再逐条输出错误。
    """
    errors = []

    def timed_out(_request, timeout):
        raise OSError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timed_out)
    monkeypatch.setattr(doh.logger, "error", errors.append)

    assert doh._doh_query("resolver.test", "example.com") is None
    assert doh.doh_query_json("resolver.test", "example.com") is None
    assert errors == []


def test_enable_doh_cools_down_after_all_resolvers_fail(monkeypatch):
    """
    所有解析器都失败后，冷却期内直接走系统 DNS 且只告警一次；冷却结束后重新查询。
    """
    clock = [1000.0]
    query_calls = []
    query_result = [None]
    resolved_hosts = []
    warnings = []

    def fake_query(resolver: str, host: str):
        query_calls.append((resolver, host))
        return query_result[0]

    def fake_getaddrinfo(host: str, *args, **kwargs):
        resolved_hosts.append(host)
        return []

    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", fake_query)
    monkeypatch.setattr(doh, "_orig_getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(doh, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doh.logger, "warning", warnings.append)

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True

        socket.getaddrinfo("example.com", None)
        assert query_calls == [("resolver.test", "example.com")]
        assert resolved_hosts == ["example.com"]
        assert len(warnings) == 1

        clock[0] += doh._DOH_FAILURE_COOLDOWN_SECONDS - 1
        socket.getaddrinfo("example.com", None)
        assert len(query_calls) == 1
        assert resolved_hosts == ["example.com", "example.com"]
        assert len(warnings) == 1

        clock[0] += 2
        query_result[0] = "203.0.113.7"
        socket.getaddrinfo("example.com", None)
        assert len(query_calls) == 2
        assert resolved_hosts[-1] == "203.0.113.7"
        assert "example.com" not in doh._doh_failure_until
        assert doh._doh_cache["example.com"] == "203.0.113.7"
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo

    assert doh._doh_cache == {}
    assert doh._doh_failure_until == {}


def test_doh_config_reload_clears_failure_cooldown(monkeypatch):
    """
    修改 DoH 配置后应清除失败冷却，让新的解析器立即生效。
    """
    query_calls = []
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr("app.runtime.config.settings.DOH_ENABLE", True)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: query_calls.append(resolver))
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])
    monkeypatch.setattr(doh.logger, "warning", lambda _message: None)

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        socket.getaddrinfo("example.com", None)
        assert "example.com" in doh._doh_failure_until

        monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "fixed.test")
        helper.on_config_changed()
        assert doh._doh_failure_until == {}

        socket.getaddrinfo("example.com", None)
        assert query_calls == ["resolver.test", "fixed.test"]
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo


def test_doh_cooldown_falls_back_without_holding_executor_lock(monkeypatch):
    """
    冷却期回退系统 DNS 时不得持有执行器锁，否则系统 DNS 阻塞会拖住有界关闭。
    """
    lock_held = []
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", lambda resolver, host: None)
    monkeypatch.setattr(
        doh,
        "_orig_getaddrinfo",
        lambda host, *args, **kwargs: lock_held.append(doh._executor_lock.locked()) or [],
    )
    monkeypatch.setattr(doh.logger, "warning", lambda _message: None)

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        socket.getaddrinfo("example.com", None)
        socket.getaddrinfo("example.com", None)
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo

    assert lock_held == [False, False]


@pytest.mark.parametrize("stale_result", [None, "203.0.113.7"])
def test_doh_reload_discards_in_flight_query_result(stale_result, monkeypatch):
    """
    配置重载期间仍在进行的旧查询结果不得写回缓存或失败冷却。
    """
    helper = object.__new__(doh.DohHelper)
    query_calls = []
    warnings = []

    def reload_during_query(resolver: str, host: str):
        query_calls.append(resolver)
        if len(query_calls) == 1:
            monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "fixed.test")
            helper.on_config_changed()
            return stale_result
        return None

    original_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr("app.runtime.config.settings.DOH_ENABLE", True)
    monkeypatch.setattr("app.runtime.config.settings.DOH_DOMAINS", "example.com")
    monkeypatch.setattr("app.runtime.config.settings.DOH_RESOLVERS", "resolver.test")
    monkeypatch.setattr(doh, "_doh_query", reload_during_query)
    monkeypatch.setattr(doh, "_orig_getaddrinfo", lambda host, *args, **kwargs: [])
    monkeypatch.setattr(doh.logger, "warning", warnings.append)

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        socket.getaddrinfo("example.com", None)
        assert doh._doh_cache == {}
        assert doh._doh_failure_until == {}
        assert warnings == []

        socket.getaddrinfo("example.com", None)
        assert query_calls == ["resolver.test", "fixed.test"]
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo


def test_doh_concurrent_failures_warn_once(monkeypatch):
    """
    同一代次的并发查询全部失败时，只有首个记录冷却的查询输出告警。
    """
    warnings = []
    original_getaddrinfo = socket.getaddrinfo
    helper = object.__new__(doh.DohHelper)
    monkeypatch.setattr(doh.logger, "warning", warnings.append)

    try:
        assert helper.shutdown() is True
        assert doh.enable_doh(True) is True
        generation = doh._doh_generation
        doh._remember_doh_result("example.com", None, generation)
        doh._remember_doh_result("example.com", None, generation)
    finally:
        helper.shutdown()
        socket.getaddrinfo = original_getaddrinfo

    assert len(warnings) == 1
