import socket
from unittest import TestCase
from unittest.mock import patch

from app.utils.security import (
    SecurityUtils,
    UrlSafetyDiagnosis,
    UrlSafetyReason,
    _dns_inflight_locks,
    _dns_negative_cache,
    _dns_positive_cache,
)


class SecurityUtilsTest(TestCase):
    def setUp(self) -> None:
        """
        每个用例前清空 DNS TTL 缓存与 in-flight 锁，避免跨用例状态污染。
        """
        _dns_positive_cache.clear()
        _dns_negative_cache.clear()
        _dns_inflight_locks.clear()

    def test_signed_url_roundtrip_returns_clean_url(self):
        """
        URL 签名验证成功后返回不含签名片段的真实请求地址。
        """
        url = "http://192.168.1.50:8096/Items/abc/Images/Primary?api_key=demo"

        signed_url = SecurityUtils.sign_url(url)

        self.assertIn("#mp_sig=", signed_url)
        self.assertIn("mp_purpose=image-proxy", signed_url)
        self.assertEqual(SecurityUtils.verify_signed_url(signed_url), url)
        self.assertEqual(SecurityUtils.strip_url_signature(signed_url), url)

    def test_signed_url_rejects_tampered_url(self):
        """
        签名绑定完整 URL，签名后修改路径必须校验失败。
        """
        signed_url = SecurityUtils.sign_url(
            "http://192.168.1.50:8096/Items/abc/Images/Primary"
        )
        tampered_url = signed_url.replace(
            "/Items/abc/Images/Primary",
            "/System/Info/Public",
        )

        self.assertIsNone(SecurityUtils.verify_signed_url(tampered_url))

    def test_signed_url_is_deterministic_for_same_inputs(self):
        """
        相同 URL 与 RESOURCE_SECRET_KEY 多次签名结果必须完全一致，
        保证浏览器 / Service Worker 缓存能稳定命中。
        """
        url = "http://192.168.1.50:8096/Items/abc/Images/Primary"

        first = SecurityUtils.sign_url(url)
        second = SecurityUtils.sign_url(url)

        self.assertEqual(first, second)
        self.assertEqual(SecurityUtils.verify_signed_url(first), url)

    def test_signed_url_invalidated_after_secret_rotation(self):
        """
        `RESOURCE_SECRET_KEY` 变更（进程重启或运维显式轮换）后旧签名必须作废，
        作为签名长期有效模型的失效兜底。
        """
        url = "http://192.168.1.50:8096/Items/abc/Images/Primary"

        with patch(
            "app.utils.security.settings.RESOURCE_SECRET_KEY",
            "old-secret-value-aaaaaaaaaaaaaaaaaaaaaaaa",
        ):
            signed_url = SecurityUtils.sign_url(url)
            self.assertEqual(SecurityUtils.verify_signed_url(signed_url), url)

        with patch(
            "app.utils.security.settings.RESOURCE_SECRET_KEY",
            "new-secret-value-bbbbbbbbbbbbbbbbbbbbbbbb",
        ):
            self.assertIsNone(SecurityUtils.verify_signed_url(signed_url))

    def test_signed_url_rejects_other_purpose(self):
        """
        签名绑定 `purpose`，挪用到其它签名用途必须被拒绝。
        """
        url = "http://192.168.1.50:8096/Items/abc/Images/Primary"
        signed_url = SecurityUtils.sign_url(url)

        self.assertIsNone(
            SecurityUtils.verify_signed_url(signed_url, purpose="other-purpose")
        )

    def test_is_safe_url_keeps_default_allowlist_behavior(self):
        """
        默认 URL 校验保持历史 allowlist 行为，避免影响非代理调用方。
        """
        self.assertTrue(
            SecurityUtils.is_safe_url(
                "http://192.168.1.50:8096/secret.png",
                {"http://192.168.1.50:8096"},
            )
        )

    def test_is_safe_url_blocks_private_literal_ip_when_enabled(self):
        """
        启用 SSRF 防护时，即使内网 IP 命中 allowlist 也不能放行。
        """
        self.assertFalse(
            SecurityUtils.is_safe_url(
                "http://192.168.1.50:8096/secret.png",
                {"http://192.168.1.50:8096"},
                block_private=True,
            )
        )

    def test_is_safe_url_blocks_loopback_dns_result_when_enabled(self):
        """
        主机名解析到回环地址时必须拒绝，防止通过域名绕过内网地址拦截。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "http://internal.example.com/secret.png",
                    {"example.com"},
                    block_private=True,
                )
            )

    def test_is_safe_url_blocks_mixed_public_and_private_dns_results(self):
        """
        同一域名只要存在任一非公网解析结果，就不能作为图片代理目标。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )

    def test_is_safe_url_allows_public_dns_result_when_enabled(self):
        """
        域名解析结果全部为公网地址且命中 allowlist 时继续允许访问。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertTrue(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )

    def test_is_safe_url_rejects_dns_resolution_failure_when_enabled(self):
        """
        SSRF 防护无法确认目标地址时按失败处理，避免解析异常时继续请求。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ):
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )

    def test_is_safe_url_allows_configured_private_range_after_domain_match(self):
        """
        图片域名命中 allowlist 后，可通过配置允许 TUN fake-ip 等特定非公网网段。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("198.18.16.96", 0),
                )
            ],
        ), patch("app.utils.security.logger.debug") as debug_log:
            self.assertTrue(
                SecurityUtils.is_safe_url(
                    "https://img1.doubanio.com/poster.webp",
                    {"doubanio.com"},
                    block_private=True,
                    allowed_private_ranges=["198.18.0.0/15"],
                )
            )
            debug_message = debug_log.call_args.args[0]
            self.assertIn("ips=198.18.16.96", debug_message)
            self.assertIn("ranges=198.18.0.0/15", debug_message)

    def test_is_safe_url_blocks_configured_private_range_without_domain_match(self):
        """
        非公网网段例外必须依附域名白名单，不能单独放行任意用户 URL。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://attacker.example.com/poster.webp",
                    {"doubanio.com"},
                    block_private=True,
                    allowed_private_ranges=["198.18.0.0/15"],
                )
            )

    def test_is_safe_url_blocks_private_result_outside_configured_range(self):
        """
        仅允许显式配置的非公网网段，其它内网解析结果仍按 SSRF 风险拦截。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                    allowed_private_ranges=["198.18.0.0/15"],
                )
            )

    def test_is_safe_url_blocks_mixed_allowed_and_disallowed_private_results(self):
        """
        同一域名的解析结果必须全部落在允许网段内，避免部分安全结果掩盖风险地址。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                    allowed_private_ranges=["198.18.0.0/15"],
                )
            )

    def test_is_safe_url_async_uses_event_loop_resolver(self):
        """
        异步版本通过事件循环的非阻塞 getaddrinfo 完成 SSRF 校验，
        且语义与同步版本保持一致：解析到非公网地址时仍然拒绝。
        """
        import asyncio

        async def fake_getaddrinfo(host, *_args, **_kwargs):
            self.assertEqual(host, "internal.example.com")
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
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
                return await SecurityUtils.is_safe_url_async(
                    "http://internal.example.com/secret.png",
                    {"example.com"},
                    block_private=True,
                )

        self.assertFalse(asyncio.run(run()))

    def test_is_safe_url_async_hits_dns_cache(self):
        """
        异步与同步版本共享 DNS TTL 缓存：同步预热后，异步版本不应再发起 DNS 查询。
        """
        import asyncio

        # 先用同步路径预热缓存
        with patch(
            "app.utils.security.socket.getaddrinfo",
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
            self.assertTrue(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )

        async def run() -> bool:
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

        self.assertTrue(asyncio.run(run()))

    def test_is_safe_url_async_allows_public_dns_result(self):
        """
        异步版本对全公网解析结果且命中 allowlist 时放行。
        """
        import asyncio

        async def fake_getaddrinfo(host, *_args, **_kwargs):
            self.assertEqual(host, "assets.example.com")
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
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
                return await SecurityUtils.is_safe_url_async(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )

        self.assertTrue(asyncio.run(run()))

    def test_dns_resolution_failure_populates_negative_cache(self):
        """
        DNS 解析失败应回填负向缓存，避免短期内对同一目标反复触发 `getaddrinfo`。
        """
        from app.utils.security import _dns_negative_cache as neg_cache

        with patch(
            "app.utils.security.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ) as mock_resolve:
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/poster.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )
            self.assertEqual(mock_resolve.call_count, 1)
            self.assertIn("assets.example.com", neg_cache)

            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "https://assets.example.com/another.jpg",
                    {"example.com"},
                    block_private=True,
                )
            )
            self.assertEqual(
                mock_resolve.call_count,
                1,
                "命中负向缓存后不应再次调用 getaddrinfo",
            )

    def test_literal_ip_skips_dns_cache(self):
        """
        URL 中的字面量 IP 走快路径，不应进入 DNS 缓存或触发 `getaddrinfo`。
        """
        from app.utils.security import (
            _dns_negative_cache as neg_cache,
            _dns_positive_cache as pos_cache,
        )

        with patch(
            "app.utils.security.socket.getaddrinfo",
            side_effect=AssertionError("字面量 IP 不应触发 getaddrinfo"),
        ):
            self.assertFalse(
                SecurityUtils.is_safe_url(
                    "http://10.0.0.5:8080/secret.png",
                    {"http://10.0.0.5:8080"},
                    block_private=True,
                )
            )
        self.assertNotIn("10.0.0.5", pos_cache)
        self.assertNotIn("10.0.0.5", neg_cache)

    def test_literal_ipv6_in_brackets_is_recognized(self):
        """
        `urlparse` 已为 IPv6 字面量脱壳，`_literal_ip` 兼容直接传入带方括号的形式。
        """
        self.assertEqual(
            str(SecurityUtils._literal_ip("[::1]")),
            "::1",
        )
        self.assertEqual(
            str(SecurityUtils._literal_ip("::1")),
            "::1",
        )
        self.assertIsNone(SecurityUtils._literal_ip("not-an-ip"))

    def test_is_safe_url_async_dedupes_concurrent_inflight_queries(self):
        """
        同 hostname 的并发未命中请求应通过 in-flight 锁去重，只触发一次 DNS 查询。
        """
        import asyncio

        call_count = 0

        async def run() -> None:
            nonlocal call_count
            loop = asyncio.get_running_loop()
            release = asyncio.Event()

            async def slow_getaddrinfo(host, *_args, **_kwargs):
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

            self.assertTrue(all(results))
            self.assertEqual(call_count, 1, "并发未命中应去重为单次 DNS 查询")

        asyncio.run(run())

    def test_sync_cache_access_is_thread_safe(self):
        """
        同步路径下并发线程访问 DNS 缓存不应触发异常或拿到不一致结果。
        TTLCache 自身非线程安全，依赖模块级 `_dns_cache_lock` 串行化读写。
        """
        import threading

        with patch(
            "app.utils.security.socket.getaddrinfo",
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
                try:
                    for _ in range(50):
                        results.append(
                            SecurityUtils.is_safe_url(
                                "https://assets.example.com/poster.jpg",
                                {"example.com"},
                                block_private=True,
                            )
                        )
                except Exception as exc:  # noqa: BLE001 - 用例需捕获任意异常
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(results))
        self.assertEqual(len(results), 8 * 50)

    def test_async_dns_resolution_failure_releases_inflight_lock(self):
        """
        DNS 解析失败后 in-flight 锁字典中必须被清理，避免每个解析失败的 hostname
        都在 `_dns_inflight_locks` 里残留一把 `asyncio.Lock`。
        """
        import asyncio

        async def fail_getaddrinfo(*_args, **_kwargs):
            raise socket.gaierror()

        async def run() -> None:
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", side_effect=fail_getaddrinfo):
                result = await SecurityUtils._hostname_addresses_async(
                    "bad-host.example"
                )
            self.assertIsNone(result)

        asyncio.run(run())
        self.assertNotIn(
            "bad-host.example",
            _dns_inflight_locks,
            "解析失败路径必须释放 in-flight 锁字典条目",
        )

    def test_async_dns_resolution_success_releases_inflight_lock(self):
        """
        正常解析完成后 in-flight 锁字典也必须被清理，避免 hostname 累积。
        """
        import asyncio

        async def fake_getaddrinfo(*_args, **_kwargs):
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
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
                result = await SecurityUtils._hostname_addresses_async(
                    "ok-host.example"
                )
            self.assertIsNotNone(result)

        asyncio.run(run())
        self.assertNotIn(
            "ok-host.example",
            _dns_inflight_locks,
            "正常解析路径必须释放 in-flight 锁字典条目",
        )

    def test_async_dns_concurrent_waiters_release_inflight_lock(self):
        """
        并发未命中场景下，所有等待者完成后 in-flight 锁字典也必须被清理，
        覆盖"等到锁但缓存已被前一个协程回填"的二次返回路径。
        """
        import asyncio

        async def run() -> None:
            loop = asyncio.get_running_loop()
            release = asyncio.Event()

            async def slow_getaddrinfo(*_args, **_kwargs):
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
                        SecurityUtils._hostname_addresses_async("multi-host.example")
                    )
                    for _ in range(5)
                ]
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()
                await asyncio.gather(*tasks)

        asyncio.run(run())
        self.assertNotIn(
            "multi-host.example",
            _dns_inflight_locks,
            "并发等待者全部退出后必须释放 in-flight 锁字典条目",
        )


class UrlSafetyDiagnosisTest(TestCase):
    """
    覆盖 `evaluate_url_safety(_async)` 的结构化诊断结果，确保每条
    `UrlSafetyReason` 分支返回的字段满足日志渲染契约。
    """

    def setUp(self) -> None:
        _dns_positive_cache.clear()
        _dns_negative_cache.clear()
        _dns_inflight_locks.clear()

    def test_domain_not_allowed_returns_reason_and_no_host(self):
        """
        协议或 allowlist 校验未通过时，诊断返回 DOMAIN_NOT_ALLOWED，
        且不暴露 host/ips 字段。
        """
        diag = SecurityUtils.evaluate_url_safety(
            "https://attacker.example.com/x.jpg",
            {"image.tmdb.org"},
        )

        self.assertIsInstance(diag, UrlSafetyDiagnosis)
        self.assertFalse(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.DOMAIN_NOT_ALLOWED)
        self.assertIsNone(diag.host)
        self.assertEqual(diag.ips, [])
        self.assertEqual(diag.matched_private_ranges, [])

    def test_allowed_without_block_private_skips_dns(self):
        """
        未启用 block_private 时直接放行，不发起 DNS 解析，ips 保持为空。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
            side_effect=AssertionError("不应触发 DNS 解析"),
        ):
            diag = SecurityUtils.evaluate_url_safety(
                "https://image.tmdb.org/t/p/w500/x.jpg",
                {"image.tmdb.org"},
            )

        self.assertTrue(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.ALLOWED)
        self.assertEqual(diag.host, "image.tmdb.org")
        self.assertEqual(diag.ips, [])

    def test_dns_resolution_failed_carries_host_without_ips(self):
        """
        `block_private=True` 下 DNS 抛错时返回 DNS_RESOLUTION_FAILED，
        附带 host 便于排查但不携带 ips。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ):
            diag = SecurityUtils.evaluate_url_safety(
                "https://image.tmdb.org/t/p/w500/x.jpg",
                {"image.tmdb.org"},
                block_private=True,
            )

        self.assertFalse(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.DNS_RESOLUTION_FAILED)
        self.assertEqual(diag.host, "image.tmdb.org")
        self.assertEqual(diag.ips, [])

    def test_non_global_dns_result_lists_resolved_ips(self):
        """
        命中 allowlist 但 DNS 解析到非公网且未配置允许网段时，诊断标记
        NON_GLOBAL_DNS_RESULT 并把解析到的 IP 列出来，供日志附带 fake-ip 提示。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
            ],
        ):
            diag = SecurityUtils.evaluate_url_safety(
                "https://image.tmdb.org/t/p/w500/x.jpg",
                {"image.tmdb.org"},
                block_private=True,
            )

        self.assertFalse(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.NON_GLOBAL_DNS_RESULT)
        self.assertEqual(diag.host, "image.tmdb.org")
        self.assertEqual(diag.ips, ["198.18.16.96"])
        self.assertEqual(diag.matched_private_ranges, [])

    def test_mixed_private_and_public_with_ranges_reports_mixed_reason(self):
        """
        配置了 allowed_private_ranges 但解析结果存在公网或不在允许网段内的私网
        地址时，诊断必须标记 MIXED_OR_DISALLOWED_PRIVATE_RESULT，避免与"未配置
        允许网段"场景混淆。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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

        self.assertFalse(diag.allowed)
        self.assertIs(
            diag.reason, UrlSafetyReason.MIXED_OR_DISALLOWED_PRIVATE_RESULT
        )
        self.assertEqual(diag.ips, ["198.18.16.96", "10.0.0.8"])

    def test_allowed_via_configured_private_range_reports_matched_networks(self):
        """
        通过 allowed_private_ranges 放行时返回 ALLOWED，同时把命中的 IP 与
        网段填入诊断对象，便于排查日志确认放行依据。
        """
        with patch(
            "app.utils.security.socket.getaddrinfo",
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

        self.assertTrue(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.ALLOWED)
        self.assertEqual(diag.ips, ["198.18.16.96"])
        self.assertEqual(diag.matched_private_ranges, ["198.18.0.0/15"])

    def test_async_evaluation_returns_same_diagnosis(self):
        """
        异步版本走事件循环线程池但应保持与同步版本一致的诊断结果。
        """
        import asyncio

        async def fake_getaddrinfo(host, *_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.16.96", 0)),
            ]

        async def run():
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
        self.assertFalse(diag.allowed)
        self.assertIs(diag.reason, UrlSafetyReason.NON_GLOBAL_DNS_RESULT)
        self.assertEqual(diag.ips, ["198.18.16.96"])
