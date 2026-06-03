import asyncio
import ipaddress
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.testing import stub_modules


def _stub(name: str, **attrs) -> tuple:
    """构造带指定属性的占位模块，返回 ``(模块名, 模块)`` 供 :func:`stub_modules` 使用。"""
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return name, module


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _DummyError(Exception):
    def __init__(self, message="", duration_ms=None):
        super().__init__(message)
        self.duration_ms = duration_ms


# 在 import 期用占位模块替换重依赖/外部模块，import 完由 stub_modules 精确还原，避免污染其它用例
_STUB_MODULES = dict([
    _stub("pillow_avif"),
    _stub("aiofiles"),
    _stub("psutil"),
    _stub("app.helper.sites", SitesHelper=_Dummy),
    _stub("app.chain.media", MediaChain=_Dummy),
    _stub("app.chain.mediaserver", MediaServerChain=_Dummy),
    _stub("app.chain.search", SearchChain=_Dummy),
    _stub("app.chain.system", SystemChain=_Dummy),
    _stub("app.core.event", eventmanager=_Dummy(), Event=_Dummy, EventManager=_Dummy),
    _stub("app.core.metainfo", MetaInfo=_Dummy),
    _stub("app.core.module", ModuleManager=_Dummy),
    _stub("app.core.security", verify_apitoken=_Dummy, verify_resource_token=_Dummy, verify_token=_Dummy),
    _stub("app.db.models", User=_Dummy),
    _stub("app.db.systemconfig_oper", SystemConfigOper=_Dummy),
    _stub("app.db.user_oper", get_current_active_superuser=_Dummy,
          get_current_active_superuser_async=_Dummy, get_current_active_user_async=_Dummy),
    _stub("app.helper.llm", LLMHelper=_Dummy, LLMTestError=_DummyError, LLMTestTimeout=_DummyError),
    _stub("app.helper.mediaserver", MediaServerHelper=_Dummy),
    _stub("app.helper.message", MessageHelper=_Dummy),
    _stub("app.helper.progress", ProgressHelper=_Dummy),
    _stub("app.helper.rule", RuleHelper=_Dummy),
    _stub("app.helper.server", MoviePilotServerHelper=_Dummy),
    _stub("app.helper.system", SystemHelper=_Dummy),
    _stub("app.helper.image", ImageHelper=_Dummy),
    _stub("app.scheduler", Scheduler=_Dummy),
    _stub("app.log", logger=_Dummy(), log_settings=_Dummy(),
          LogConfigModel=type("LogConfigModel", (), {})),
    _stub("app.utils.crypto", HashUtils=_Dummy),
    _stub("app.utils.http", RequestUtils=_Dummy, AsyncRequestUtils=_Dummy),
    _stub("version", APP_VERSION="test", FRONTEND_VERSION="frontend-test"),
])

with stub_modules(_STUB_MODULES):
    from app.api.endpoints import system as system_endpoint


class NettestSecurityTest(unittest.TestCase):
    def test_get_env_setting_reports_rust_available_and_enabled_separately(self):
        """
        系统配置接口应分别返回 Rust 扩展可用性和当前实际启用状态。
        """
        with patch.object(system_endpoint.rust_accel, "is_available", return_value=True), patch.object(
            system_endpoint.rust_accel, "is_enabled", return_value=False
        ):
            resp = asyncio.run(system_endpoint.get_env_setting(_="token"))

        self.assertTrue(resp.success)
        self.assertTrue(resp.data["RUST_ACCEL_AVAILABLE"])
        self.assertFalse(resp.data["RUST_ACCEL_ENABLED"])

    def test_fetch_image_allows_signed_private_url(self):
        """
        服务端签名过的私网图片 URL 可以继续代理，保证前端封面显示。
        """
        image_url = "http://192.168.1.50:8096/System/Info/Public"
        signed_url = system_endpoint.SecurityUtils.sign_url(image_url)
        image_helper = Mock()
        image_helper.async_fetch_image = AsyncMock(return_value=b"image-bytes")

        with patch.object(system_endpoint, "ImageHelper", return_value=image_helper), patch.object(
            system_endpoint.HashUtils, "md5", return_value="etag", create=True
        ), patch.object(
            system_endpoint.RequestUtils, "generate_cache_headers", return_value={}, create=True
        ):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url=signed_url,
                    allowed_domains=set(),
                )
            )

        self.assertEqual(resp.status_code, 200)
        image_helper.async_fetch_image.assert_awaited_once_with(
            url=image_url,
            proxy=None,
            use_cache=False,
            cookies=None,
        )

    def test_fetch_image_blocks_private_allowed_url_before_request(self):
        """
        图片代理即使拿到内网 allowlist 项，也必须在发起请求前拦截。
        """
        class FailIfCalled:
            def __init__(self, *args, **kwargs):
                raise AssertionError("fetch_image should block private URLs before fetching")

        with patch.object(system_endpoint, "ImageHelper", FailIfCalled):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url="http://127.0.0.1:8096/secret.png",
                    allowed_domains={"http://127.0.0.1:8096"},
                )
            )

        self.assertIsNone(resp)

    def test_fetch_image_allows_configured_private_range_after_domain_match(self):
        """
        图片代理在域名白名单命中后，可按配置放行指定非公网解析网段。
        """
        image_helper = Mock()
        image_helper.async_fetch_image = AsyncMock(return_value=b"image-bytes")

        with patch.object(system_endpoint, "ImageHelper", return_value=image_helper), patch.object(
            system_endpoint.HashUtils, "md5", return_value="etag", create=True
        ), patch.object(
            system_endpoint.RequestUtils, "generate_cache_headers", return_value={}, create=True
        ), patch.object(
            # is_safe_image_url_async 经 evaluate_url_safety_async 走异步解析
            # _hostname_addresses_async（loop.getaddrinfo）；必须 mock 异步版本，
            # 否则真实 DNS 逃逸到 img1.doubanio.com，且私网放行分支根本不会被执行到。
            system_endpoint.SecurityUtils,
            "_hostname_addresses_async",
            new=AsyncMock(return_value=[ipaddress.ip_address("198.18.16.96")]),
        ), patch.object(
            system_endpoint.settings,
            "IMAGE_PROXY_ALLOWED_PRIVATE_RANGES",
            ["198.18.0.0/15"],
        ), patch(
            "app.utils.security.logger.debug",
        ):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url="https://img1.doubanio.com/poster.webp",
                    allowed_domains={"doubanio.com"},
                )
            )

        self.assertEqual(resp.status_code, 200)
        image_helper.async_fetch_image.assert_awaited_once_with(
            url="https://img1.doubanio.com/poster.webp",
            proxy=None,
            use_cache=False,
            cookies=None,
        )

    def test_fetch_image_blocks_tampered_signed_private_url(self):
        """
        私网签名绑定完整 URL，改动路径后不能继续代理。
        """
        signed_url = system_endpoint.SecurityUtils.sign_url(
            "http://192.168.1.50:8096/Items/abc/Images/Primary"
        ).replace("/Items/abc/Images/Primary", "/System/Info/Public")

        class FailIfCalled:
            def __init__(self, *args, **kwargs):
                raise AssertionError("fetch_image should block tampered signed URLs")

        with patch.object(system_endpoint, "ImageHelper", FailIfCalled):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url=signed_url,
                    allowed_domains=set(),
                )
            )

        self.assertIsNone(resp)

    def test_nettest_targets_are_served_by_backend(self):
        resp = asyncio.run(system_endpoint.nettest_targets(_="token"))

        self.assertTrue(resp.success)
        self.assertTrue(any(item["id"] == "pip_proxy" for item in resp.data))
        self.assertTrue(any(item["id"] == "github_proxy_web" for item in resp.data))

    def test_nettest_blocks_unknown_target(self):
        class FailIfCalled:
            def __init__(self, *args, **kwargs):
                raise AssertionError("nettest should reject unknown targets before any outbound request")

        with patch.object(system_endpoint, "AsyncRequestUtils", FailIfCalled):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="unknown-target",
                    _="token",
                )
            )

        self.assertFalse(resp.success)
        self.assertIn("不存在", resp.message)

    def test_nettest_blocks_unapproved_redirect(self):
        captured = {"calls": 0}

        class FakeResponse:
            def __init__(self, status_code, headers=None, text=""):
                self.status_code = status_code
                self.headers = headers or {}
                self.text = text

            async def aclose(self):
                return None

        class FakeAsyncRequestUtils:
            def __init__(self, **kwargs):
                captured["init_kwargs"] = kwargs

            async def get_res(self, url, allow_redirects=True):
                captured["calls"] += 1
                return FakeResponse(
                    302,
                    headers={"location": "https://169.254.169.254/latest/meta-data/"},
                )

        with patch.object(system_endpoint, "AsyncRequestUtils", FakeAsyncRequestUtils), patch.object(
            system_endpoint.settings,
            "GITHUB_PROXY",
            "https://ghproxy.example/",
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="github_proxy_web",
                    _="token",
                )
            )

        self.assertFalse(resp.success)
        self.assertIn("跳转", resp.message)
        self.assertEqual(captured["calls"], 1)

    def test_nettest_allows_known_external_redirects(self):
        cases = {
            "telegram_api": "https://core.telegram.org/bots",
            "douban_api": "https://www.douban.com/doubanapp/frodo?wechat=0&os=Other",
            "github_codeload": "https://github.com/",
        }

        for target_id, redirect_url in cases.items():
            call_urls = []

            class FakeResponse:
                def __init__(self, status_code, headers=None, text=""):
                    self.status_code = status_code
                    self.headers = headers or {}
                    self.text = text

                async def aclose(self):
                    return None

            class FakeAsyncRequestUtils:
                def __init__(self, **kwargs):
                    pass

                async def get_res(self, url, allow_redirects=True):
                    call_urls.append(url)
                    if len(call_urls) == 1:
                        return FakeResponse(302, headers={"location": redirect_url})
                    return FakeResponse(200, text="ok")

            with self.subTest(target_id=target_id), patch.object(
                system_endpoint,
                "AsyncRequestUtils",
                FakeAsyncRequestUtils,
            ):
                resp = asyncio.run(
                    system_endpoint.nettest(
                        target_id=target_id,
                        _="token",
                    )
                )

            self.assertTrue(resp.success)
            self.assertEqual(len(call_urls), 2)

    def test_nettest_uses_safe_http_options_and_server_side_content_check(self):
        captured = {}

        class FakeAsyncRequestUtils:
            def __init__(self, **kwargs):
                captured["init_kwargs"] = kwargs

            async def get_res(self, url, allow_redirects=True):
                captured["url"] = url
                captured["allow_redirects"] = allow_redirects
                return SimpleNamespace(status_code=200, text="MoviePilot README")

        with patch.object(system_endpoint, "AsyncRequestUtils", FakeAsyncRequestUtils), patch.object(
            system_endpoint.settings,
            "GITHUB_PROXY",
            "https://ghproxy.example/",
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="github_proxy_web",
                    include="tag_name",
                    _="token",
                )
            )

        self.assertTrue(resp.success)
        self.assertEqual(
            captured["url"],
            "https://ghproxy.example/https://github.com/jxxghp/MoviePilot/blob/v2/README.md",
        )
        self.assertFalse(captured["allow_redirects"])
        self.assertTrue(captured["init_kwargs"]["verify"])
        self.assertFalse(captured["init_kwargs"]["follow_redirects"])

    def test_nettest_fails_when_expected_content_is_missing(self):
        class FakeAsyncRequestUtils:
            def __init__(self, **kwargs):
                pass

            async def get_res(self, url, allow_redirects=True):
                return SimpleNamespace(status_code=200, text="proxy landing page")

        with patch.object(system_endpoint, "AsyncRequestUtils", FakeAsyncRequestUtils), patch.object(
            system_endpoint.settings,
            "PIP_PROXY",
            "https://pypi.tuna.tsinghua.edu.cn/simple/",
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="pip_proxy",
                    _="token",
                )
            )

        self.assertFalse(resp.success)
        self.assertIn("PIP加速代理", resp.message)
