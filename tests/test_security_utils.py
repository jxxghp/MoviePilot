import socket
from unittest import TestCase
from unittest.mock import patch

from app.utils.security import SecurityUtils


class SecurityUtilsTest(TestCase):
    def test_signed_url_roundtrip_returns_clean_url(self):
        """
        URL 签名验证成功后返回不含签名片段的真实请求地址。
        """
        url = "http://192.168.1.50:8096/Items/abc/Images/Primary?api_key=demo"

        signed_url = SecurityUtils.sign_url(url)

        self.assertIn("#mp_exp=", signed_url)
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

    def test_signed_url_rejects_expired_signature(self):
        """
        已过期签名不能继续放行私网图片代理请求。
        """
        with patch("app.utils.security.time.time", return_value=1000):
            signed_url = SecurityUtils.sign_url(
                "http://192.168.1.50:8096/Items/abc/Images/Primary",
                expires_in=10,
            )

        with patch("app.utils.security.time.time", return_value=1011):
            self.assertIsNone(SecurityUtils.verify_signed_url(signed_url))

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
