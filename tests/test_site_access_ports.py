"""站点访问 Application Port 的装配与兼容回归。"""

import base64
from types import SimpleNamespace

import pytest

from app.application import rss as rss_module
from app.application import torrent as torrent_module
from app.application.rss import RssHelper, configure_rss_ports
from app.application.security import cookie as cookie_module
from app.application.security.cookie import CookieHelper, configure_cookie_ports
from app.application.torrent import (
    TorrentHelper,
    configure_torrent_port,
)
from app.startup.initializers import site as site_initializer


class _UnusedPort:
    """拒绝测试用例未声明的端口调用。"""

    def __getattr__(self, _name):
        """让意外边界调用立即暴露。"""
        raise AssertionError("不应调用该测试端口")


class _EmptyCache:
    """隔离种子下载测试的文件缓存。"""

    @staticmethod
    def get(*_args, **_kwargs):
        """固定返回未命中。"""
        return None

    @staticmethod
    def set(*_args, **_kwargs):
        """拒绝失败响应写入缓存。"""
        raise AssertionError("失败响应不应写入缓存")


@pytest.fixture(autouse=True)
def reset_ports():
    """保证每个用例前后均无跨测试端口状态。"""
    site_initializer.reset_site_access_ports()
    yield
    site_initializer.reset_site_access_ports()


def test_rss_fake_ports_preserve_http_failure_tristate() -> None:
    """RSS HTTP 非 200 仍返回 False，不把技术边迁移变成异常。"""

    class HttpPort:
        """返回站点限流响应的 RSS HTTP 假端口。"""

        @staticmethod
        def get(**_kwargs):
            """返回非成功响应。"""
            return SimpleNamespace(status_code=429, content=b"", text="", reason="rate")

    configure_rss_ports(http=HttpPort(), browser=_UnusedPort(), parser=_UnusedPort())

    assert RssHelper().parse("https://example.test/rss") is False


@pytest.mark.parametrize("render", [False, True])
def test_rss_link_uses_selected_fake_access_port(render: bool) -> None:
    """RSS 链接发现应按站点配置选择 HTTP 或浏览器窄端口。"""
    calls: list[str] = []

    class HttpPort:
        """返回包含 RSS 地址的配置页面。"""

        @staticmethod
        def post(**_kwargs):
            """记录 HTTP 表单请求并返回页面。"""
            calls.append("http")
            return SimpleNamespace(
                status_code=200,
                content=b"",
                text='<a class="faqlink" href="https://feed.test/rss">rss</a>',
                reason="",
            )

    class BrowserPort:
        """返回包含 RSS 地址的渲染页面。"""

        @staticmethod
        def render(**_kwargs):
            """记录浏览器渲染并返回页面。"""
            calls.append("browser")
            return '<a href="https://feed.test/rss">rss</a>'

    configure_rss_ports(
        http=HttpPort(), browser=BrowserPort(), parser=_UnusedPort()
    )
    url = "https://zhuque.in/" if render else "https://example.test/"

    link, message = RssHelper().get_rss_link(url, "cookie", "ua")

    assert link == "https://feed.test/rss"
    assert message == ""
    assert calls == ["browser" if render else "http"]


def test_torrent_fake_port_preserves_rate_limit_message(monkeypatch) -> None:
    """种子下载限流分支继续保留旧五元组与中文错误。"""

    class HttpPort:
        """返回 429 的种子 HTTP 假端口。"""

        @staticmethod
        def request(**_kwargs):
            """返回站点限流响应。"""
            return SimpleNamespace(
                status_code=429, headers={}, content=b"", text="", reason="rate"
            )

    monkeypatch.setattr(torrent_module, "FileCache", _EmptyCache)
    configure_torrent_port(HttpPort())

    result = TorrentHelper().download_torrent("https://example.test/download/1")

    assert result[1:] == (None, "", [], "触发站点流控，请稍后重试")


def test_cookie_invalid_parameters_keep_legacy_result_without_ports() -> None:
    """参数错误属于 Application 语义，不应依赖浏览器端口已装配。"""
    assert CookieHelper().get_site_cookie_ua("", "", "") == (
        None,
        None,
        "参数错误",
    )


def test_cookie_captcha_uses_http_and_ocr_fake_ports() -> None:
    """验证码下载与识别只通过各自窄端口传递图片内容。"""
    received: list[str] = []

    class HttpPort:
        """返回固定验证码图片。"""

        @staticmethod
        def fetch(**_kwargs):
            """提供验证码图片字节。"""
            return b"captcha-image"

    class OcrPort:
        """记录 OCR 收到的 Base64 内容。"""

        @staticmethod
        def recognize(image_b64: str) -> str:
            """记录输入并返回识别结果。"""
            received.append(image_b64)
            return "A1B2"

    configure_cookie_ports(
        browser=_UnusedPort(), http=HttpPort(), ocr=OcrPort()
    )

    result = CookieHelper._CookieHelper__get_captcha_text(
        cookie="sid=1", ua="ua", code_url="https://example.test/captcha"
    )

    assert result == "A1B2"
    assert received == [base64.b64encode(b"captcha-image").decode()]


def test_reset_ports_make_real_access_fail_explicitly(monkeypatch) -> None:
    """reset 后真实访问必须明确失败，不得在 Application 隐式回建 Adapter。"""
    monkeypatch.setattr(torrent_module, "FileCache", _EmptyCache)

    with pytest.raises(RuntimeError, match="RSS 访问端口"):
        RssHelper().parse("https://example.test/rss")
    with pytest.raises(RuntimeError, match="站点登录端口"):
        CookieHelper().get_site_cookie_ua(
            "https://example.test", "user", "password"
        )
    with pytest.raises(RuntimeError, match="种子下载端口"):
        TorrentHelper().download_torrent("https://example.test/download/2")


def test_initializer_rolls_back_partial_ports_on_failure(monkeypatch) -> None:
    """中途装配失败时必须清除先前成功的 RSS Port。"""

    def fail_cookie_configuration(**_kwargs) -> None:
        """模拟第二组端口装配失败。"""
        raise RuntimeError("injected failure")

    monkeypatch.setattr(
        site_initializer, "configure_cookie_ports", fail_cookie_configuration
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        site_initializer.init_site_access_ports()

    with pytest.raises(RuntimeError, match="RSS 访问端口"):
        rss_module._require_rss_ports()


def test_initializer_configures_and_reset_releases_all_ports() -> None:
    """统一 initializer 应一次装配并一次释放三组 Application Port。"""
    site_initializer.init_site_access_ports()

    assert rss_module._require_rss_ports()
    assert cookie_module._require_cookie_ports()
    assert torrent_module._require_torrent_port()

    site_initializer.reset_site_access_ports()

    with pytest.raises(RuntimeError):
        rss_module._require_rss_ports()
    with pytest.raises(RuntimeError):
        cookie_module._require_cookie_ports()
    with pytest.raises(RuntimeError):
        torrent_module._require_torrent_port()
