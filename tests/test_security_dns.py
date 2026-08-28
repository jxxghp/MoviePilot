"""安全 URL 的 DNS Port 与系统适配器回归测试。"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Optional

import pytest

from app.adapters.network.resolver import SocketDnsResolver
from app.application.security import url as security_url
from app.application.security.url import (
    DnsResolver,
    SecurityUtils,
    configure_dns_resolver,
    reset_dns_resolver,
)
from app.sdk.network import SecurityUtils as SdkSecurityUtils


class _FakeResolver:
    """记录同步和异步调用并返回固定地址的测试 DNS 端口。"""

    def __init__(self, addresses: Optional[Sequence[str]]) -> None:
        """保存固定解析结果。"""
        self.addresses = addresses
        self.sync_calls = 0
        self.async_calls = 0

    def resolve(self, hostname: str) -> Optional[Sequence[str]]:
        """记录同步解析调用。"""
        assert hostname
        self.sync_calls += 1
        return self.addresses

    async def async_resolve(self, hostname: str) -> Optional[Sequence[str]]:
        """记录异步解析调用。"""
        assert hostname
        self.async_calls += 1
        return self.addresses


@pytest.fixture
def restore_dns_resolver() -> Iterator[None]:
    """用例结束后恢复测试引导已装配的系统 resolver。"""
    previous = configure_dns_resolver(_FakeResolver(("93.184.216.34",)))
    yield
    reset_dns_resolver(previous)


def test_dns_port_is_runtime_checkable_by_structure() -> None:
    """系统适配器必须满足 Application 声明的窄端口。"""
    resolver: DnsResolver = SocketDnsResolver()
    assert callable(resolver.resolve)
    assert callable(resolver.async_resolve)


def test_security_utils_sdk_identity_and_zero_argument_construction(
    restore_dns_resolver: None,
) -> None:
    """插件 SDK 继续导出同一 SecurityUtils 类，零参构造仍可调用公开方法。"""
    assert SdkSecurityUtils is SecurityUtils
    assert SecurityUtils().is_safe_url(
        "https://assets.example.com/poster.jpg",
        {"example.com"},
        block_private=True,
    )


def test_unconfigured_dns_resolver_fails_without_lazy_adapter_import(
    restore_dns_resolver: None,
) -> None:
    """未装配 DNS 端口时 hostname 校验应稳定失败，不得懒加载 Adapter。"""
    reset_dns_resolver()

    assert not SecurityUtils.is_safe_url(
        "https://assets.example.com/poster.jpg",
        {"example.com"},
        block_private=True,
    )
    with pytest.raises(RuntimeError, match="DNS 解析器尚未由启动组合根装配"):
        SecurityUtils._hostname_addresses("assets.example.com")


def test_reconfigure_dns_resolver_clears_cached_result(
    restore_dns_resolver: None,
) -> None:
    """切换 resolver 后不得继续使用旧实现填充的安全结果。"""
    public = _FakeResolver(("93.184.216.34",))
    private = _FakeResolver(("127.0.0.1",))
    configure_dns_resolver(public)
    assert SecurityUtils.is_safe_url(
        "https://assets.example.com/poster.jpg",
        {"example.com"},
        block_private=True,
    )

    configure_dns_resolver(private)

    assert not SecurityUtils.is_safe_url(
        "https://assets.example.com/poster.jpg",
        {"example.com"},
        block_private=True,
    )
    assert public.sync_calls == 1
    assert private.sync_calls == 1


def test_async_security_check_uses_only_async_dns_port(
    restore_dns_resolver: None,
) -> None:
    """异步安全校验不得回退同步 DNS I/O。"""
    resolver = _FakeResolver(("93.184.216.34",))
    configure_dns_resolver(resolver)

    result = asyncio.run(
        SecurityUtils.is_safe_url_async(
            "https://assets.example.com/poster.jpg",
            {"example.com"},
            block_private=True,
        )
    )

    assert result
    assert resolver.sync_calls == 0
    assert resolver.async_calls == 1


def test_async_dns_inflight_locks_are_isolated_between_event_loop_threads(
    restore_dns_resolver: None,
) -> None:
    """两个线程各自运行 event loop 时不得共享 asyncio.Lock 或互相挂死。"""
    rendezvous = threading.Barrier(2)
    state_lock = threading.Lock()

    class _TwoLoopResolver(_FakeResolver):
        """要求两个事件循环同时进入解析端口的并发测试 resolver。"""

        def __init__(self) -> None:
            """初始化解析计数和事件循环身份集合。"""
            super().__init__(("93.184.216.34",))
            self.loop_ids: set[int] = set()

        async def async_resolve(self, hostname: str) -> Optional[Sequence[str]]:
            """记录当前 loop，并等待另一线程也进入解析端口。"""
            assert hostname
            with state_lock:
                self.async_calls += 1
                self.loop_ids.add(id(asyncio.get_running_loop()))
            rendezvous.wait(timeout=2)
            await asyncio.sleep(0)
            return self.addresses

    resolver = _TwoLoopResolver()
    configure_dns_resolver(resolver)
    results: list[bool] = []
    errors: list[BaseException] = []

    def run_check() -> None:
        """在当前工作线程独立创建并关闭一个事件循环。"""
        try:
            result = asyncio.run(
                SecurityUtils.is_safe_url_async(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )
        except BaseException as error:  # noqa: BLE001 - 测试需收集线程异常
            errors.append(error)
        else:
            results.append(result)

    workers = [threading.Thread(target=run_check, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert results == [True, True]
    assert resolver.async_calls == 2
    assert len(resolver.loop_ids) == 2
    assert security_url._dns_inflight_locks == {}


def test_reconfigure_during_resolution_rejects_stale_cache_write(
    restore_dns_resolver: None,
) -> None:
    """旧 resolver 的迟到结果不得覆盖并发装配的新 resolver 缓存。"""
    started = threading.Event()
    release = threading.Event()

    class _BlockingResolver(_FakeResolver):
        """等待测试放行后才返回旧代际地址。"""

        def resolve(self, hostname: str) -> Optional[Sequence[str]]:
            """通知查询已开始，并等待 resolver 切换完成。"""
            self.sync_calls += 1
            started.set()
            assert release.wait(timeout=2)
            return self.addresses

    old_resolver = _BlockingResolver(("93.184.216.34",))
    new_resolver = _FakeResolver(("127.0.0.1",))
    configure_dns_resolver(old_resolver)
    first_result: list[bool] = []
    worker = threading.Thread(
        target=lambda: first_result.append(
            SecurityUtils.is_safe_url(
                "https://assets.example.com/poster.jpg",
                {"example.com"},
                block_private=True,
            )
        )
    )
    worker.start()
    assert started.wait(timeout=2)

    configure_dns_resolver(new_resolver)
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert first_result == [True]
    assert not SecurityUtils.is_safe_url(
        "https://assets.example.com/backdrop.jpg",
        {"example.com"},
        block_private=True,
    )
    assert new_resolver.sync_calls == 1


def test_failed_dns_port_result_is_negative_cached(
    restore_dns_resolver: None,
) -> None:
    """解析失败仍进入负向缓存，避免短期内重复调用 Adapter。"""
    resolver = _FakeResolver(None)
    configure_dns_resolver(resolver)

    for path in ("poster.jpg", "backdrop.jpg"):
        assert not SecurityUtils.is_safe_url(
            f"https://assets.example.com/{path}",
            {"example.com"},
            block_private=True,
        )

    assert resolver.sync_calls == 1


def test_socket_dns_resolver_sync_normalizes_all_addresses(monkeypatch) -> None:
    """同步 Adapter 返回全部规范地址，不在 Application 内执行 socket I/O。"""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2001:4860:4860::8888", 0)),
        ],
    )

    assert SocketDnsResolver().resolve("assets.example.com") == (
        "93.184.216.34",
        "2001:4860:4860::8888",
    )


def test_socket_dns_resolver_async_uses_current_loop(monkeypatch) -> None:
    """异步 Adapter 使用事件循环 resolver，并把系统失败规范为 None。"""
    async def run() -> None:
        """在当前循环内替换 resolver，确保测试不触发真实 DNS。"""
        loop = asyncio.get_running_loop()

        async def fail(*_args, **_kwargs):
            """模拟系统 DNS 失败。"""
            raise socket.gaierror()

        monkeypatch.setattr(loop, "getaddrinfo", fail)
        assert await SocketDnsResolver().async_resolve("missing.example") is None

    asyncio.run(run())


def test_application_security_url_has_no_direct_dns_operation() -> None:
    """Application 安全模块不得重新调用 socket 或事件循环 getaddrinfo。"""
    source = Path(security_url.__file__).read_text(encoding="utf-8")
    assert "socket.getaddrinfo(" not in source
    assert ".getaddrinfo(" not in source
