"""
覆盖 `SecurityUtils.is_safe_image_url_async` 的阻断分支与日志聚合接线：

- 各 `UrlSafetyReason` 分支落入正确的 warning 字段；
- `NON_GLOBAL_DNS_RESULT` 且未配置允许网段时附 fake-ip 提示；
- 签名 URL 校验通过时静默放行；URL 携带签名但失败时附 `invalid_signature` 标记；
- 同 (host, reason) 高频拦截只输出首条 warning，窗口结束输出聚合摘要；
- 不同 (host, reason) 互不吞并。
"""

import asyncio
from typing import List, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from app.utils import security as security_module
from app.utils.coalesce import EventCoalescer
from app.utils.security import (
    SecurityUtils,
    UrlSafetyDiagnosis,
    UrlSafetyReason,
)


_TEST_WINDOW = 0.05
_TEST_WAIT = _TEST_WINDOW * 4


def _diag(
    reason: UrlSafetyReason,
    *,
    host: Optional[str] = "image.tmdb.org",
    ips: Optional[List[str]] = None,
) -> UrlSafetyDiagnosis:
    """
    构造测试用 `UrlSafetyDiagnosis`：DOMAIN_NOT_ALLOWED 强制清空 host，保持与
    `evaluate_url_safety_async` 真实输出的字段约束一致。
    """
    if reason is UrlSafetyReason.DOMAIN_NOT_ALLOWED:
        host = None
    return UrlSafetyDiagnosis(
        allowed=False,
        reason=reason,
        host=host,
        ips=ips or [],
    )


class IsSafeImageUrlLogTest(IsolatedAsyncioTestCase):
    """
    `is_safe_image_url_async` 阻断路径的结构化日志 + 聚合行为校验。
    """

    async def asyncSetUp(self) -> None:
        # 用短窗口实例临时替换模块级 coalescer，便于在测试内驱动窗口到期 flush
        self._original_coalescer = security_module._image_proxy_block_log_coalescer
        self._coalescer = EventCoalescer(
            window_seconds=_TEST_WINDOW,
            on_flush=security_module._log_image_proxy_block_summary,
            source="image_proxy_test",
        )
        security_module._image_proxy_block_log_coalescer = self._coalescer
        self._allowed_domains = {"image.tmdb.org"}

    async def asyncTearDown(self) -> None:
        await self._coalescer.close()
        security_module._image_proxy_block_log_coalescer = self._original_coalescer

    async def _invoke(
        self,
        diagnosis: UrlSafetyDiagnosis,
        *,
        url: str = "https://image.tmdb.org/t/p/w500/x.jpg",
        signed_clean_url: Optional[str] = None,
        allowed_private_ranges: Optional[List[str]] = None,
    ):
        """
        以指定诊断结果与签名校验返回值驱动 `is_safe_image_url_async`，捕获 warning。
        """
        async def fake_evaluate(*_args, **_kwargs):
            return diagnosis

        warns: List[str] = []
        with patch.object(
            SecurityUtils,
            "evaluate_url_safety_async",
            side_effect=fake_evaluate,
        ), patch.object(
            SecurityUtils,
            "verify_signed_url",
            return_value=signed_clean_url,
        ), patch.object(
            security_module.logger,
            "warn",
            side_effect=warns.append,
        ):
            allowed = await SecurityUtils.is_safe_image_url_async(
                url,
                self._allowed_domains,
                allowed_private_ranges=allowed_private_ranges,
            )
        return allowed, warns

    async def test_domain_not_allowed_emits_clean_reason_label(self):
        """
        普通外链（未携带 mp_sig）撞 allowlist 失败时，warning 标记
        DOMAIN_NOT_ALLOWED，不附 fake-ip 提示，也不挂签名失败标记，
        避免误导未签名调用方以为必须签名。
        """
        allowed, warns = await self._invoke(
            _diag(UrlSafetyReason.DOMAIN_NOT_ALLOWED),
        )

        self.assertFalse(allowed)
        self.assertEqual(len(warns), 1)
        self.assertIn("reason=domain_not_allowed", warns[0])
        self.assertIn("Blocked unsafe image URL", warns[0])
        self.assertNotIn("fake-ip", warns[0])
        self.assertNotIn("invalid_signature", warns[0])

    async def test_invalid_signature_tag_only_when_url_signed(self):
        """
        URL 显式携带 `#mp_sig=...` 但校验失败时，reason 末尾追加
        `invalid_signature`，便于区分"签名失效"与"未签名外链拦截"。
        """
        allowed, warns = await self._invoke(
            _diag(UrlSafetyReason.DOMAIN_NOT_ALLOWED),
            url="https://attacker.example.com/x.jpg#mp_sig=deadbeef&mp_purpose=image-proxy",
        )

        self.assertFalse(allowed)
        self.assertEqual(len(warns), 1)
        self.assertIn(
            "reason=domain_not_allowed+invalid_signature", warns[0]
        )

    async def test_non_global_dns_result_lists_ips_with_hint(self):
        """
        DNS 解析到非公网且未配置允许网段时，warning 列出解析 IP 并附 fake-ip 提示。
        """
        allowed, warns = await self._invoke(
            _diag(
                UrlSafetyReason.NON_GLOBAL_DNS_RESULT,
                ips=["198.18.16.96", "198.18.16.97"],
            ),
        )

        self.assertFalse(allowed)
        self.assertEqual(len(warns), 1)
        warning = warns[0]
        self.assertIn("reason=non_global_dns_result", warning)
        self.assertIn("host=image.tmdb.org", warning)
        self.assertIn("ips=198.18.16.96,198.18.16.97", warning)
        self.assertIn("IMAGE_PROXY_ALLOWED_PRIVATE_RANGES", warning)
        self.assertIn("198.18.0.0/15", warning)

    async def test_configured_ranges_skip_fakeip_hint(self):
        """
        已配置 allowed_private_ranges 时不再追加 fake-ip 提示，避免重复引导。
        warning 同时把已生效的网段列在字段里供运维对照。
        """
        _, warns = await self._invoke(
            _diag(
                UrlSafetyReason.MIXED_OR_DISALLOWED_PRIVATE_RESULT,
                ips=["10.0.0.8"],
            ),
            allowed_private_ranges=["198.18.0.0/15"],
        )

        self.assertEqual(len(warns), 1)
        warning = warns[0]
        self.assertIn("reason=mixed_or_disallowed_private_result", warning)
        self.assertIn("allowed_private_ranges=198.18.0.0/15", warning)
        self.assertNotIn("提示", warning)

    async def test_dns_resolution_failed_carries_empty_ips(self):
        """
        DNS 解析失败的 warning 携带空 ips 字段，便于运维直接定位 DNS 路径。
        """
        _, warns = await self._invoke(
            _diag(UrlSafetyReason.DNS_RESOLUTION_FAILED, ips=[]),
        )

        self.assertEqual(len(warns), 1)
        self.assertIn("reason=dns_resolution_failed", warns[0])
        self.assertIn("ips=,", warns[0])

    async def test_signed_url_success_silently_allows(self):
        """
        标准校验失败但签名 URL 校验通过时返回 True，且不输出 warning，
        避免运维误判后端预签名路径是异常拦截。
        """
        allowed, warns = await self._invoke(
            _diag(UrlSafetyReason.DOMAIN_NOT_ALLOWED),
            signed_clean_url="https://image.tmdb.org/t/p/w500/x.jpg",
        )

        self.assertTrue(allowed)
        self.assertEqual(warns, [])

    async def test_repeated_block_in_window_emits_only_first_warning(self):
        """
        同 (host, reason) 在窗口内的多次命中只输出首条 warning；窗口到期后
        补一条聚合摘要，count 等于窗口内总命中数，sample_url 来自首条事件。
        """
        diag = _diag(
            UrlSafetyReason.NON_GLOBAL_DNS_RESULT,
            ips=["198.18.16.96"],
        )

        async def fake_evaluate(*_args, **_kwargs):
            return diag

        warns: List[str] = []
        with patch.object(
            SecurityUtils,
            "evaluate_url_safety_async",
            side_effect=fake_evaluate,
        ), patch.object(
            SecurityUtils,
            "verify_signed_url",
            return_value=None,
        ), patch.object(
            security_module.logger,
            "warn",
            side_effect=warns.append,
        ):
            for i in range(5):
                await SecurityUtils.is_safe_image_url_async(
                    f"https://image.tmdb.org/t/p/w500/{i}.jpg",
                    self._allowed_domains,
                )
            self.assertEqual(len(warns), 1)
            self.assertIn("/0.jpg", warns[0])

            await asyncio.sleep(_TEST_WAIT)

        self.assertEqual(len(warns), 2)
        summary = warns[1]
        self.assertIn("aggregated", summary)
        self.assertIn("count=5", summary)
        self.assertIn("/0.jpg", summary)
        self.assertNotIn("/1.jpg", summary)
        self.assertNotIn("/4.jpg", summary)
        # 摘要附带首条样例的解析 IP，便于直接锁定批量拦截的网络成因
        self.assertIn("sample_ips=198.18.16.96", summary)

    async def test_different_keys_do_not_collapse(self):
        """
        不同 (host, reason) 各自计数与输出，互不吞并。
        """
        warns: List[str] = []
        sequence = {
            "evil": _diag(UrlSafetyReason.DOMAIN_NOT_ALLOWED, host=None),
            "tmdb": _diag(
                UrlSafetyReason.NON_GLOBAL_DNS_RESULT,
                host="image.tmdb.org",
                ips=["198.18.16.96"],
            ),
        }

        async def fake_evaluate(url, *_args, **_kwargs):
            return sequence["evil"] if "evil" in url else sequence["tmdb"]

        with patch.object(
            SecurityUtils,
            "evaluate_url_safety_async",
            side_effect=fake_evaluate,
        ), patch.object(
            SecurityUtils,
            "verify_signed_url",
            return_value=None,
        ), patch.object(
            security_module.logger,
            "warn",
            side_effect=warns.append,
        ):
            await SecurityUtils.is_safe_image_url_async(
                "https://evil.example.com/x.jpg",
                self._allowed_domains,
            )
            await SecurityUtils.is_safe_image_url_async(
                "https://image.tmdb.org/t/p/w500/a.jpg",
                self._allowed_domains,
            )

        self.assertEqual(len(warns), 2)
        self.assertIn("reason=domain_not_allowed", warns[0])
        self.assertIn("reason=non_global_dns_result", warns[1])
