"""
验证 URL 签名、地址安全判断及 DNS 缓存和并发解析。
"""

import socket
from unittest.mock import patch

import pytest

from app.application.security.url import (
    SecurityUtils,
    UrlSafetyDiagnosis,
    UrlSafetyReason,
    _dns_inflight_locks,
    _dns_negative_cache,
    _dns_positive_cache,
)


@pytest.fixture
def _isolate_dns_state():
    """
    在空缓存下运行 DNS 校验，并在用例结束时恢复原有缓存和锁映射。
    """
    caches = (_dns_positive_cache, _dns_negative_cache, _dns_inflight_locks)
    originals = [dict(cache) for cache in caches]
    for cache in caches:
        cache.clear()
    try:
        yield
    finally:
        for cache, original in zip(caches, originals):
            cache.clear()
            cache.update(original)


@pytest.mark.usefixtures("_isolate_dns_state")
def test_signed_url_roundtrip_returns_clean_url() -> None:
    """
    URL 签名验证成功后返回不含签名片段的真实请求地址。
    """
    url = "http://192.168.1.50:8096/Items/abc/Images/Primary?api_key=demo"

    signed_url = SecurityUtils.sign_url(url)

    assert "#mp_sig=" in signed_url
    assert "mp_purpose=image-proxy" in signed_url
    assert SecurityUtils.verify_signed_url(signed_url) == url
    assert SecurityUtils.strip_url_signature(signed_url) == url


@pytest.mark.usefixtures("_isolate_dns_state")
def test_signed_url_rejects_tampered_url() -> None:
    """
    签名绑定完整 URL，签名后修改路径必须校验失败。
    """
    signed_url = SecurityUtils.sign_url("http://192.168.1.50:8096/Items/abc/Images/Primary")
    tampered_url = signed_url.replace(
        "/Items/abc/Images/Primary",
        "/System/Info/Public",
    )

    assert SecurityUtils.verify_signed_url(tampered_url) is None


@pytest.mark.usefixtures("_isolate_dns_state")
def test_signed_url_is_deterministic_for_same_inputs() -> None:
    """
    相同 URL 与 RESOURCE_SECRET_KEY 多次签名结果必须完全一致，
    保证浏览器 / Service Worker 缓存能稳定命中。
    """
    url = "http://192.168.1.50:8096/Items/abc/Images/Primary"

    first = SecurityUtils.sign_url(url)
    second = SecurityUtils.sign_url(url)

    assert first == second
    assert SecurityUtils.verify_signed_url(first) == url


@pytest.mark.usefixtures("_isolate_dns_state")
def test_signed_url_invalidated_after_secret_rotation() -> None:
    """
    `RESOURCE_SECRET_KEY` 变更（进程重启或运维显式轮换）后旧签名必须作废，
    作为签名长期有效模型的失效兜底。
    """
    url = "http://192.168.1.50:8096/Items/abc/Images/Primary"

    with patch(
        "app.runtime.config.settings.RESOURCE_SECRET_KEY",
        "old-secret-value-aaaaaaaaaaaaaaaaaaaaaaaa",
    ):
        signed_url = SecurityUtils.sign_url(url)
        assert SecurityUtils.verify_signed_url(signed_url) == url

    with patch(
        "app.runtime.config.settings.RESOURCE_SECRET_KEY",
        "new-secret-value-bbbbbbbbbbbbbbbbbbbbbbbb",
    ):
        assert SecurityUtils.verify_signed_url(signed_url) is None


@pytest.mark.usefixtures("_isolate_dns_state")
def test_signed_url_rejects_other_purpose() -> None:
    """
    签名绑定 `purpose`，挪用到其它签名用途必须被拒绝。
    """
    url = "http://192.168.1.50:8096/Items/abc/Images/Primary"
    signed_url = SecurityUtils.sign_url(url)

    assert SecurityUtils.verify_signed_url(signed_url, purpose="other-purpose") is None


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_keeps_default_allowlist_behavior() -> None:
    """
    默认 URL 校验保持历史 allowlist 行为，避免影响非代理调用方。
    """
    assert SecurityUtils.is_safe_url("http://192.168.1.50:8096/secret.png", {"http://192.168.1.50:8096"})


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_private_literal_ip_when_enabled() -> None:
    """
    启用 SSRF 防护时，即使内网 IP 命中 allowlist 也不能放行。
    """
    assert not SecurityUtils.is_safe_url(
        "http://192.168.1.50:8096/secret.png", {"http://192.168.1.50:8096"}, block_private=True
    )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_loopback_dns_result_when_enabled() -> None:
    """
    主机名解析到回环地址时必须拒绝，防止通过域名绕过内网地址拦截。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("127.0.0.1", 0),
            )
        ],
    ):
        assert not SecurityUtils.is_safe_url(
            "http://internal.example.com/secret.png", {"example.com"}, block_private=True
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_mixed_public_and_private_dns_results() -> None:
    """
    同一域名只要存在任一非公网解析结果，就不能作为图片代理目标。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("10.0.0.8", 0),
            ),
        ],
    ):
        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/poster.jpg", {"example.com"}, block_private=True
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_allows_public_dns_result_when_enabled() -> None:
    """
    域名解析结果全部为公网地址且命中 allowlist 时继续允许访问。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            )
        ],
    ):
        assert SecurityUtils.is_safe_url("https://assets.example.com/poster.jpg", {"example.com"}, block_private=True)


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_rejects_dns_resolution_failure_when_enabled() -> None:
    """
    SSRF 防护无法确认目标地址时按失败处理，避免解析异常时继续请求。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        side_effect=socket.gaierror,
    ):
        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/poster.jpg", {"example.com"}, block_private=True
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_allows_configured_private_range_after_domain_match() -> None:
    """
    图片域名命中 allowlist 后，可通过配置允许 TUN fake-ip 等特定非公网网段。
    """
    with (
        patch(
            "app.adapters.network.resolver.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("198.18.16.96", 0),
                )
            ],
        ),
        patch("app.application.security.url.logger.debug") as debug_log,
    ):
        assert SecurityUtils.is_safe_url(
            "https://img1.doubanio.com/poster.webp",
            {"doubanio.com"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )
        debug_message = debug_log.call_args.args[0]
        assert "ips=198.18.16.96" in debug_message
        assert "ranges=198.18.0.0/15" in debug_message


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_configured_private_range_without_domain_match() -> None:
    """
    非公网网段例外必须依附域名白名单，不能单独放行任意用户 URL。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("198.18.16.96", 0),
            )
        ],
    ):
        assert not SecurityUtils.is_safe_url(
            "https://attacker.example.com/poster.webp",
            {"doubanio.com"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_private_result_outside_configured_range() -> None:
    """
    仅允许显式配置的非公网网段，其它内网解析结果仍按 SSRF 风险拦截。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("10.0.0.8", 0),
            )
        ],
    ):
        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/poster.jpg",
            {"example.com"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_blocks_mixed_allowed_and_disallowed_private_results() -> None:
    """
    同一域名的解析结果必须全部落在允许网段内，避免部分安全结果掩盖风险地址。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("198.18.16.96", 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("10.0.0.8", 0),
            ),
        ],
    ):
        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/poster.jpg",
            {"example.com"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_async_uses_event_loop_resolver() -> None:
    """
    异步版本通过事件循环的非阻塞 getaddrinfo 完成 SSRF 校验，
    且语义与同步版本保持一致：解析到非公网地址时仍然拒绝。
    """
    import asyncio

    async def fake_getaddrinfo(host, *_args, **_kwargs):
        """
        返回预设 DNS 地址，隔离真实域名解析并记录调用。
        """
        assert host == "internal.example.com"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("127.0.0.1", 0),
            )
        ]

    async def run() -> bool:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
            return await SecurityUtils.is_safe_url_async(
                "http://internal.example.com/secret.png",
                {"example.com"},
                block_private=True,
            )

    assert not asyncio.run(run())


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_async_hits_dns_cache() -> None:
    """
    异步与同步版本共享 DNS TTL 缓存：同步预热后，异步版本不应再发起 DNS 查询。
    """
    import asyncio

    # 先用同步路径预热缓存
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            )
        ],
    ):
        assert SecurityUtils.is_safe_url("https://assets.example.com/poster.jpg", {"example.com"}, block_private=True)

    async def run() -> bool:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            side_effect=AssertionError("缓存命中后不应再次发起 DNS 查询"),
        ):
            return await SecurityUtils.is_safe_url_async(
                "https://assets.example.com/poster.jpg",
                {"example.com"},
                block_private=True,
            )

    assert asyncio.run(run())


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_async_allows_public_dns_result() -> None:
    """
    异步版本对全公网解析结果且命中 allowlist 时放行。
    """
    import asyncio

    async def fake_getaddrinfo(host, *_args, **_kwargs):
        """
        返回预设 DNS 地址，隔离真实域名解析并记录调用。
        """
        assert host == "assets.example.com"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            )
        ]

    async def run() -> bool:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
            return await SecurityUtils.is_safe_url_async(
                "https://assets.example.com/poster.jpg",
                {"example.com"},
                block_private=True,
            )

    assert asyncio.run(run())


@pytest.mark.usefixtures("_isolate_dns_state")
def test_dns_resolution_failure_populates_negative_cache() -> None:
    """
    DNS 解析失败应回填负向缓存，避免短期内对同一目标反复触发 `getaddrinfo`。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        side_effect=socket.gaierror,
    ) as mock_resolve:
        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/poster.jpg", {"example.com"}, block_private=True
        )
        assert mock_resolve.call_count == 1
        assert "assets.example.com" in _dns_negative_cache

        assert not SecurityUtils.is_safe_url(
            "https://assets.example.com/another.jpg", {"example.com"}, block_private=True
        )
        assert mock_resolve.call_count == 1, "命中负向缓存后不应再次调用 getaddrinfo"


@pytest.mark.usefixtures("_isolate_dns_state")
def test_literal_ip_skips_dns_cache() -> None:
    """
    URL 中的字面量 IP 走快路径，不应进入 DNS 缓存或触发 `getaddrinfo`。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        side_effect=AssertionError("字面量 IP 不应触发 getaddrinfo"),
    ):
        assert not SecurityUtils.is_safe_url(
            "http://10.0.0.5:8080/secret.png", {"http://10.0.0.5:8080"}, block_private=True
        )
    assert "10.0.0.5" not in _dns_positive_cache
    assert "10.0.0.5" not in _dns_negative_cache


@pytest.mark.usefixtures("_isolate_dns_state")
def test_literal_ipv6_in_brackets_is_recognized() -> None:
    """
    `urlparse` 已为 IPv6 字面量脱壳，`_literal_ip` 兼容直接传入带方括号的形式。
    """
    assert str(SecurityUtils._literal_ip("[::1]")) == "::1"
    assert str(SecurityUtils._literal_ip("::1")) == "::1"
    assert SecurityUtils._literal_ip("not-an-ip") is None


@pytest.mark.usefixtures("_isolate_dns_state")
def test_is_safe_url_async_dedupes_concurrent_inflight_queries() -> None:
    """
    同 hostname 的并发未命中请求应通过 in-flight 锁去重，只触发一次 DNS 查询。
    """
    import asyncio

    call_count = 0

    async def run() -> None:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        nonlocal call_count
        loop = asyncio.get_running_loop()
        release = asyncio.Event()

        async def slow_getaddrinfo(host, *_args, **_kwargs):
            """
            等待测试信号后返回 DNS 地址，用于观察并发合并和取消行为。
            """
            nonlocal call_count
            call_count += 1
            await release.wait()
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("93.184.216.34", 0),
                )
            ]

        with patch.object(loop, "getaddrinfo", side_effect=slow_getaddrinfo):
            tasks = [
                asyncio.create_task(
                    SecurityUtils.is_safe_url_async(
                        "https://assets.example.com/poster.jpg",
                        {"example.com"},
                        block_private=True,
                    )
                )
                for _ in range(5)
            ]
            # 让所有任务都进入 in-flight 等待状态
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks)

        assert all(results)
        assert call_count == 1, "并发未命中应去重为单次 DNS 查询"

    asyncio.run(run())


@pytest.mark.usefixtures("_isolate_dns_state")
def test_sync_cache_access_is_thread_safe() -> None:
    """
    同步路径下并发线程访问 DNS 缓存不应触发异常或拿到不一致结果。
    TTLCache 自身非线程安全，依赖模块级 `_dns_cache_lock` 串行化读写。
    """
    import threading

    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            )
        ],
    ):
        results: list = []
        errors: list = []

        def worker() -> None:
            """
            在线程中执行本次地址解析并记录结果。
            """
            try:
                for _ in range(50):
                    results.append(
                        SecurityUtils.is_safe_url(
                            "https://assets.example.com/poster.jpg",
                            {"example.com"},
                            block_private=True,
                        )
                    )
            # 捕获解析线程传播的所有异常，验证取消路径不向调用方泄漏失败。
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    assert all(results)
    assert len(results) == 8 * 50


@pytest.mark.usefixtures("_isolate_dns_state")
def test_async_dns_resolution_failure_releases_inflight_lock() -> None:
    """
    DNS 解析失败后 in-flight 锁字典中必须被清理，避免每个解析失败的 hostname
    都在 `_dns_inflight_locks` 里残留一把 `asyncio.Lock`。
    """
    import asyncio

    async def fail_getaddrinfo(*_args, **_kwargs):
        """
        模拟 DNS 解析失败，验证异常路径释放资源。
        """
        raise socket.gaierror()

    async def run() -> None:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fail_getaddrinfo):
            result = await SecurityUtils._hostname_addresses_async("bad-host.example")
        assert result is None

    asyncio.run(run())
    assert not any((key[1] == "bad-host.example" for key in _dns_inflight_locks)), (
        "解析失败路径必须释放 in-flight 锁字典条目"
    )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_async_dns_resolution_success_releases_inflight_lock() -> None:
    """
    正常解析完成后 in-flight 锁字典也必须被清理，避免 hostname 累积。
    """
    import asyncio

    async def fake_getaddrinfo(*_args, **_kwargs):
        """
        返回预设 DNS 地址，隔离真实域名解析并记录调用。
        """
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", 0),
            )
        ]

    async def run() -> None:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
            result = await SecurityUtils._hostname_addresses_async("ok-host.example")
        assert result is not None

    asyncio.run(run())
    assert not any((key[1] == "ok-host.example" for key in _dns_inflight_locks)), (
        "正常解析路径必须释放 in-flight 锁字典条目"
    )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_async_dns_concurrent_waiters_release_inflight_lock() -> None:
    """
    并发未命中场景下，所有等待者完成后 in-flight 锁字典也必须被清理，
    覆盖"等到锁但缓存已被前一个协程回填"的二次返回路径。
    """
    import asyncio

    async def run() -> None:
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        loop = asyncio.get_running_loop()
        release = asyncio.Event()

        async def slow_getaddrinfo(*_args, **_kwargs):
            """
            等待测试信号后返回 DNS 地址，用于观察并发合并和取消行为。
            """
            await release.wait()
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("93.184.216.34", 0),
                )
            ]

        with patch.object(loop, "getaddrinfo", side_effect=slow_getaddrinfo):
            tasks = [
                asyncio.create_task(SecurityUtils._hostname_addresses_async("multi-host.example")) for _ in range(5)
            ]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(run())
    assert not any((key[1] == "multi-host.example" for key in _dns_inflight_locks)), (
        "并发等待者全部退出后必须释放 in-flight 锁字典条目"
    )


@pytest.mark.usefixtures("_isolate_dns_state")
def test_domain_not_allowed_returns_reason_and_no_host() -> None:
    """
    协议或 allowlist 校验未通过时，诊断返回 DOMAIN_NOT_ALLOWED，
    且不暴露 host/ips 字段。
    """
    diag = SecurityUtils.evaluate_url_safety(
        "https://attacker.example.com/x.jpg",
        {"image.tmdb.org"},
    )

    assert isinstance(diag, UrlSafetyDiagnosis)
    assert not diag.allowed
    assert diag.reason is UrlSafetyReason.DOMAIN_NOT_ALLOWED
    assert diag.host is None
    assert diag.ips == []
    assert diag.matched_private_ranges == []


@pytest.mark.usefixtures("_isolate_dns_state")
def test_allowed_without_block_private_skips_dns() -> None:
    """
    未启用 block_private 时直接放行，不发起 DNS 解析，ips 保持为空。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        side_effect=AssertionError("不应触发 DNS 解析"),
    ):
        diag = SecurityUtils.evaluate_url_safety(
            "https://image.tmdb.org/t/p/w500/x.jpg",
            {"image.tmdb.org"},
        )

    assert diag.allowed
    assert diag.reason is UrlSafetyReason.ALLOWED
    assert diag.host == "image.tmdb.org"
    assert diag.ips == []


@pytest.mark.usefixtures("_isolate_dns_state")
def test_dns_resolution_failed_carries_host_without_ips() -> None:
    """
    `block_private=True` 下 DNS 抛错时返回 DNS_RESOLUTION_FAILED，
    附带 host 便于排查但不携带 ips。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        side_effect=socket.gaierror,
    ):
        diag = SecurityUtils.evaluate_url_safety(
            "https://image.tmdb.org/t/p/w500/x.jpg",
            {"image.tmdb.org"},
            block_private=True,
        )

    assert not diag.allowed
    assert diag.reason is UrlSafetyReason.DNS_RESOLUTION_FAILED
    assert diag.host == "image.tmdb.org"
    assert diag.ips == []


@pytest.mark.usefixtures("_isolate_dns_state")
def test_non_global_dns_result_lists_resolved_ips() -> None:
    """
    命中 allowlist 但 DNS 解析到非公网且未配置允许网段时，诊断标记
    NON_GLOBAL_DNS_RESULT 并把解析到的 IP 列出来，供日志附带 fake-ip 提示。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
        ],
    ):
        diag = SecurityUtils.evaluate_url_safety(
            "https://image.tmdb.org/t/p/w500/x.jpg",
            {"image.tmdb.org"},
            block_private=True,
        )

    assert not diag.allowed
    assert diag.reason is UrlSafetyReason.NON_GLOBAL_DNS_RESULT
    assert diag.host == "image.tmdb.org"
    assert diag.ips == ["198.18.16.96"]
    assert diag.matched_private_ranges == []


@pytest.mark.usefixtures("_isolate_dns_state")
def test_mixed_private_and_public_with_ranges_reports_mixed_reason() -> None:
    """
    配置了 allowed_private_ranges 但解析结果存在公网或不在允许网段内的私网
    地址时，诊断必须标记 MIXED_OR_DISALLOWED_PRIVATE_RESULT，避免与"未配置
    允许网段"场景混淆。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.8", 0)),
        ],
    ):
        diag = SecurityUtils.evaluate_url_safety(
            "https://image.tmdb.org/t/p/w500/x.jpg",
            {"image.tmdb.org"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )

    assert not diag.allowed
    assert diag.reason is UrlSafetyReason.MIXED_OR_DISALLOWED_PRIVATE_RESULT
    assert diag.ips == ["198.18.16.96", "10.0.0.8"]


@pytest.mark.usefixtures("_isolate_dns_state")
def test_allowed_via_configured_private_range_reports_matched_networks() -> None:
    """
    通过 allowed_private_ranges 放行时返回 ALLOWED，同时把命中的 IP 与
    网段填入诊断对象，便于排查日志确认放行依据。
    """
    with patch(
        "app.adapters.network.resolver.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
        ],
    ):
        diag = SecurityUtils.evaluate_url_safety(
            "https://image.tmdb.org/t/p/w500/x.jpg",
            {"image.tmdb.org"},
            block_private=True,
            allowed_private_ranges=["198.18.0.0/15"],
        )

    assert diag.allowed
    assert diag.reason is UrlSafetyReason.ALLOWED
    assert diag.ips == ["198.18.16.96"]
    assert diag.matched_private_ranges == ["198.18.0.0/15"]


@pytest.mark.usefixtures("_isolate_dns_state")
def test_async_evaluation_returns_same_diagnosis() -> None:
    """
    异步版本走事件循环线程池但应保持与同步版本一致的诊断结果。
    """
    import asyncio

    async def fake_getaddrinfo(host, *_args, **_kwargs):
        """
        返回预设 DNS 地址，隔离真实域名解析并记录调用。
        """
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
        ]

    async def run():
        """
        在独立事件循环中执行本用例的异步地址校验，并验证缓存或锁状态。
        """
        with patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            side_effect=fake_getaddrinfo,
            create=True,
        ):
            return await SecurityUtils.evaluate_url_safety_async(
                "https://image.tmdb.org/x.jpg",
                {"image.tmdb.org"},
                block_private=True,
            )

    diag = asyncio.run(run())
    assert not diag.allowed
    assert diag.reason is UrlSafetyReason.NON_GLOBAL_DNS_RESULT
    assert diag.ips == ["198.18.16.96"]
