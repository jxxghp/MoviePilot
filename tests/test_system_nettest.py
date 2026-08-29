import asyncio
import ipaddress
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.runtime.config import settings as runtime_settings
from app.testing import stub_modules
from app.testing.stub import restore_modules, snapshot_modules


def _stub(name: str, **attrs) -> tuple:
    """构造带指定属性的占位模块，返回 ``(模块名, 模块)`` 供 :func:`stub_modules` 使用。"""
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return name, module


class _Dummy:
    """为端点导入期依赖提供可调用的最小占位实现。"""

    def __init__(self, *args, **kwargs):
        """接受任意构造参数以模拟不同依赖类型。"""
        pass

    def __getattr__(self, _name):
        """为未显式声明的属性返回无副作用调用桩。"""
        return lambda *args, **kwargs: None


class _DummyError(Exception):
    """模拟带耗时字段的 Agent 测试异常。"""

    def __init__(self, message="", duration_ms=None):
        """保存异常消息和可选耗时。"""
        super().__init__(message)
        self.duration_ms = duration_ms


# 被测模块会绑定 import 期的桩对象，退出后需同时还原这期间加载的 app 模块图。
_STUB_MODULES = dict([
    _stub("pillow_avif"),
    _stub("aiofiles"),
    _stub("psutil"),
    _stub("app.application.site.sites", SitesHelper=_Dummy),
    _stub("app.chain.media", MediaChain=_Dummy),
    _stub("app.chain.mediaserver", MediaServerChain=_Dummy),
    _stub("app.chain.search", SearchChain=_Dummy),
    _stub("app.chain.system", SystemChain=_Dummy),
    _stub("app.runtime.events", eventmanager=_Dummy(), Event=_Dummy, EventManager=_Dummy),
    _stub("app.domain.metainfo", MetaInfo=_Dummy),
    _stub("app.runtime.extensions.module_manager", ModuleManager=_Dummy),
    _stub("app.adapters.web.security.access", verify_apitoken=_Dummy, verify_resource_token=_Dummy, verify_token=_Dummy),
    _stub("app.api.context", get_host_runtime=_Dummy),
    _stub("app.startup.composition.context", HostRuntime=_Dummy),
    _stub("app.db.models", User=_Dummy),
    _stub("app.db.oper.systemconfig", SystemConfigOper=_Dummy),
    _stub("app.api.dependencies.auth", get_current_active_superuser=_Dummy,
          get_current_active_superuser_async=_Dummy, get_current_active_user_async=_Dummy),
    _stub("app.agent.llm", LLMHelper=_Dummy, LLMTestError=_DummyError, LLMTestTimeout=_DummyError),
    _stub("app.application.mediaserver", MediaServerHelper=_Dummy),
    _stub("app.application.messaging.message", MessageHelper=_Dummy),
    _stub("app.runtime.progress", ProgressHelper=_Dummy, AsyncProgressHelper=_Dummy),
    _stub("app.application.rules", RuleHelper=_Dummy),
    _stub("app.adapters.external.server", MoviePilotServerHelper=_Dummy),
    _stub("app.runtime.state", SystemHelper=_Dummy),
    _stub("app.application.image", ImageHelper=_Dummy),
    _stub("app.scheduler", Scheduler=_Dummy),
    _stub("app.runtime.log", logger=_Dummy(), log_settings=_Dummy(),
          LogConfigModel=type("LogConfigModel", (), {})),
    _stub("app.foundation.crypto", HashUtils=_Dummy),
    _stub("app.adapters.network.http", RequestUtils=_Dummy, AsyncRequestUtils=_Dummy),
    _stub("version", APP_VERSION="test", FRONTEND_VERSION="frontend-test"),
])

_APP_MODULES = snapshot_modules("app")
try:
    with stub_modules(_STUB_MODULES):
        from app.api.endpoints import system as system_endpoint
        from app.application.network import NetworkTestService
finally:
    restore_modules(_APP_MODULES, "app")


def _network_test_service(transport, **settings) -> NetworkTestService:
    """构造不访问真实网络且读取独立设置快照的网络测试服务。"""
    values = {
        "NORMAL_USER_AGENT": "MoviePilot-Test",
        **settings,
    }
    return NetworkTestService(
        transport=transport,
        settings=lambda key, default=None: values.get(key, default),
        logger=_Dummy(),
    )


class TestNettestSecurity:
    """验证 System 安全入口和网络测试应用边界。"""

    def test_get_env_setting_reports_rust_available_and_enabled_separately(self):
        """
        系统配置接口应分别返回 Rust 扩展可用性和当前实际启用状态。
        """
        with patch.object(system_endpoint.rust_accel, "is_available", return_value=True), patch.object(
            system_endpoint.rust_accel, "is_enabled", return_value=False
        ), patch.object(
            system_endpoint.rust_accel, "is_required", return_value=True
        ), patch.object(
            system_endpoint, "is_free_threaded_runtime", return_value=True
        ), patch.object(
            system_endpoint, "is_gil_enabled", return_value=False
        ):
            resp = asyncio.run(system_endpoint.get_env_setting(_="token"))

        assert resp.success
        assert resp.data["RUST_ACCEL_AVAILABLE"]
        assert not resp.data["RUST_ACCEL_ENABLED"]
        assert resp.data["RUST_ACCEL_REQUIRED"]
        assert resp.data["PYTHON_FREE_THREADED"]
        assert not resp.data["PYTHON_GIL_ENABLED"]

    def test_get_user_global_setting_reports_runtime_variant(self):
        """登录后的全局设置应提供导航所需的解释器类型。"""
        runtime_config = SimpleNamespace(
            snapshot=Mock(return_value={}),
            get=Mock(return_value=False),
        )
        with patch.object(
            system_endpoint, "get_runtime_settings", return_value=runtime_config
        ), patch.object(
            system_endpoint.MoviePilotServerHelper,
            "async_is_admin_user",
            new=AsyncMock(return_value=False),
            create=True,
        ), patch.object(
            system_endpoint.MoviePilotServerHelper,
            "get_user_uuid",
            return_value="user-id",
            create=True,
        ), patch.object(
            system_endpoint, "is_free_threaded_runtime", return_value=True
        ), patch.object(
            system_endpoint, "is_gil_enabled", return_value=False
        ):
            resp = asyncio.run(system_endpoint.get_user_global_setting(_="token"))

        assert resp.success
        assert resp.data["PYTHON_FREE_THREADED"]
        assert not resp.data["PYTHON_GIL_ENABLED"]

    def test_fetch_image_allows_signed_private_url(self):
        """
        服务端签名过的私网图片 URL 可以继续代理，保证前端封面显示。
        """
        image_url = "http://192.168.1.50:8096/System/Info/Public"
        signed_url = system_endpoint.SecurityUtils.sign_url(image_url)
        image_helper = Mock()
        image_helper.async_fetch_image_with_mime_type = AsyncMock(
            return_value=(b"image-bytes", "image/jpeg")
        )

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

        assert resp.status_code == 200
        image_helper.async_fetch_image_with_mime_type.assert_awaited_once_with(
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
            """确保危险图片地址不会物化请求实现。"""

            def __init__(self, *args, **kwargs):
                """任何构造都表示测试失败。"""
                raise AssertionError("fetch_image should block private URLs before fetching")

        with patch.object(system_endpoint, "ImageHelper", FailIfCalled):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url="http://127.0.0.1:8096/secret.png",
                    allowed_domains={"http://127.0.0.1:8096"},
                )
            )

        assert resp is None

    def test_nettest_targets_are_served_by_application(self):
        """目标接口只投影应用服务提供的公开目录字段。"""
        service = _network_test_service(_Dummy())
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            resp = asyncio.run(system_endpoint.nettest_targets(_="token"))

        assert resp.success
        assert any(item["id"] == "pip_proxy" for item in resp.data)
        assert any(item["id"] == "github_proxy_web" for item in resp.data)
        assert all(set(item) == {"id", "name", "icon"} for item in resp.data)

    def test_nettest_blocks_unknown_target_before_transport(self):
        """未知目标在应用服务目录匹配阶段即返回，不得触发传输端口。"""

        class FailTransport:
            """确保未知目标不会进入传输边界。"""

            async def get(self, *_args, **_kwargs):
                """任何请求都表示目录准入失效。"""
                raise AssertionError("未知网络测试目标不应触发外部请求")

        service = _network_test_service(FailTransport())
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="unknown-target",
                    _="token",
                )
            )

        assert not resp.success
        assert "不存在" in resp.message

    def test_nettest_blocks_unapproved_redirect(self):
        """应用服务必须在第二次请求前阻断目标规则之外的跳转。"""
        captured = {"calls": 0, "closed": 0}

        class FakeResponse:
            """返回未授权重定向并记录资源释放。"""

            status_code = 302
            headers = {"location": "https://169.254.169.254/latest/meta-data/"}
            text = ""

            async def aclose(self):
                """记录应用服务已关闭被拒绝的响应。"""
                captured["closed"] += 1

        class FakeTransport:
            """记录未授权重定向场景中的请求次数。"""

            async def get(self, _url, **_kwargs):
                """返回固定的元数据服务跳转响应。"""
                captured["calls"] += 1
                return FakeResponse()

        service = _network_test_service(
            FakeTransport(),
            GITHUB_PROXY="https://ghproxy.example/",
        )
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="github_proxy_web",
                    _="token",
                )
            )

        assert not resp.success
        assert "跳转" in resp.message
        assert captured == {"calls": 1, "closed": 1}

    def test_nettest_allows_known_external_redirects(self):
        """内置目标声明的跨域跳转继续可用，并释放每个响应对象。"""
        cases = {
            "telegram_api": "https://core.telegram.org/bots",
            "douban_api": "https://www.douban.com/doubanapp/frodo?wechat=0&os=Other",
            "github_codeload": "https://github.com/",
        }

        for target_id, redirect_url in cases.items():
            call_urls = []
            closed = []

            class FakeResponse:
                """模拟可关闭的重定向或成功响应。"""

                def __init__(self, status_code, headers=None, text=""):
                    """保存网络测试所需的最小响应字段。"""
                    self.status_code = status_code
                    self.headers = headers or {}
                    self.text = text

                async def aclose(self):
                    """记录当前响应已被应用服务释放。"""
                    closed.append(self.status_code)

            class FakeTransport:
                """先返回受信跳转，再返回成功响应。"""

                async def get(self, url, **_kwargs):
                    """按调用顺序生成两段受控响应。"""
                    call_urls.append(url)
                    if len(call_urls) == 1:
                        return FakeResponse(302, headers={"location": redirect_url})
                    return FakeResponse(200, text="ok")

            service = _network_test_service(FakeTransport())
            with patch.object(
                system_endpoint,
                "get_configured_network_test_service",
                return_value=service,
            ):
                resp = asyncio.run(
                    system_endpoint.nettest(
                        target_id=target_id,
                        _="token",
                    )
                )

            assert resp.success, target_id
            assert len(call_urls) == 2
            assert closed == [302, 200]

    def test_nettest_rejects_malformed_port_and_path_prefix_confusion(self):
        """畸形端口和相似路径不得绕过逐目标重定向白名单。"""
        redirect_urls = (
            "https://www.douban.com:99999/doubanapp/frodo",
            "https://www.douban.com/doubanapp/frodoevil",
        )

        for redirect_url in redirect_urls:
            class FakeResponse:
                """返回当前恶意重定向并允许应用服务释放资源。"""

                status_code = 302
                headers = {"location": redirect_url}
                text = ""

                async def aclose(self):
                    """模拟关闭已读取的重定向响应。"""

            class FakeTransport:
                """只允许应用服务发起第一跳请求。"""

                def __init__(self) -> None:
                    """初始化请求计数。"""
                    self.calls = 0

                async def get(self, _url, **_kwargs):
                    """返回恶意跳转，第二次调用即表示准入失败。"""
                    self.calls += 1
                    if self.calls > 1:
                        raise AssertionError("恶意重定向不应触发第二跳请求")
                    return FakeResponse()

            transport = FakeTransport()
            service = _network_test_service(transport)
            result = asyncio.run(service.execute(target_id="douban_api"))

            assert not result.success
            assert "跳转" in result.message
            assert transport.calls == 1

    def test_nettest_uses_configured_transport_options_and_content_rule(self):
        """应用服务固定代理、请求头和内容校验，不信任旧 include 参数。"""
        captured = {}

        class FakeTransport:
            """捕获应用服务传给传输端口的受控参数。"""

            async def get(self, url, **kwargs):
                """记录请求并返回包含固定校验文本的响应。"""
                captured["url"] = url
                captured.update(kwargs)
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text="MoviePilot README",
                )

        service = _network_test_service(
            FakeTransport(),
            GITHUB_PROXY="https://ghproxy.example/",
            PROXY={"https": "http://proxy.example:7890"},
            GITHUB_HEADERS={"Authorization": "Bearer token"},
        )
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="github_proxy_web",
                    include="tag_name",
                    _="token",
                )
            )

        assert resp.success
        assert captured["url"] == (
            "https://ghproxy.example/"
            "https://github.com/jxxghp/MoviePilot/blob/v2/README.md"
        )
        assert captured["proxy"] == {"https": "http://proxy.example:7890"}
        assert captured["headers"] == {"Authorization": "Bearer token"}
        assert captured["user_agent"] == "MoviePilot-Test"

    def test_nettest_fails_when_expected_content_is_missing(self):
        """代理返回成功状态但缺少服务端固定内容时仍应判定失效。"""

        class FakeTransport:
            """返回缺少预期内容的成功状态响应。"""

            async def get(self, _url, **_kwargs):
                """模拟代理落地页冒充目标内容。"""
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text="proxy landing page",
                )

        service = _network_test_service(
            FakeTransport(),
            PIP_PROXY="https://pypi.tuna.tsinghua.edu.cn/simple/",
        )
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            resp = asyncio.run(
                system_endpoint.nettest(
                    target_id="pip_proxy",
                    _="token",
                )
            )

        assert not resp.success
        assert "PIP加速代理" in resp.message

    def test_nettest_preserves_exact_legacy_url_matching(self):
        """旧客户端传入内置 URL 时仍可解析，任意 URL 不会扩大准入面。"""

        class FakeTransport:
            """为旧 URL 精确匹配提供无网络成功响应。"""

            async def get(self, _url, **_kwargs):
                """返回满足无需内容校验目标的最小响应。"""
                return SimpleNamespace(status_code=200, headers={}, text="ok")

        service = _network_test_service(FakeTransport())
        with patch.object(
            system_endpoint,
            "get_configured_network_test_service",
            return_value=service,
        ):
            allowed = asyncio.run(
                system_endpoint.nettest(
                    url="https://api.github.com",
                    _="token",
                )
            )
            denied = asyncio.run(
                system_endpoint.nettest(
                    url="https://example.com",
                    _="token",
                )
            )

        assert allowed.success
        assert not denied.success

    def test_fetch_image_allows_configured_private_range_after_domain_match(self):
        """
        图片代理在域名白名单命中后，可按配置放行指定非公网解析网段。
        """
        image_helper = Mock()
        image_helper.async_fetch_image_with_mime_type = AsyncMock(
            return_value=(b"image-bytes", "image/jpeg")
        )

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
            runtime_settings,
            "IMAGE_PROXY_ALLOWED_PRIVATE_RANGES",
            ["198.18.0.0/15"],
        ), patch(
            "app.application.security.url.logger.debug",
        ):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url="https://img1.doubanio.com/poster.webp",
                    allowed_domains={"doubanio.com"},
                )
            )

        assert resp.status_code == 200
        image_helper.async_fetch_image_with_mime_type.assert_awaited_once_with(
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
            """确保篡改签名的私网图片不会物化请求实现。"""

            def __init__(self, *args, **kwargs):
                """任何构造都表示签名校验被绕过。"""
                raise AssertionError("fetch_image should block tampered signed URLs")

        with patch.object(system_endpoint, "ImageHelper", FailIfCalled):
            resp = asyncio.run(
                system_endpoint.fetch_image(
                    url=signed_url,
                    allowed_domains=set(),
                )
            )

        assert resp is None
