"""SiteChain 技术端口、资源所有权与装配原子性回归。"""

import ast
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional

import pytest

import app.application.torrent.download as torrent_module
from app.adapters.external import cookiecloud as cookiecloud_module
from app.application import rss as rss_module
from app.application.security import cookie as cookie_module
from app.application.site.contract import SiteSnapshot
from app.chain import site as site_module
from app.chain.site import SiteChain, configure_site_ports, reset_site_ports
from app.startup.composition import site as site_composition
from app.startup.initializers import site as site_initializer


class _FakeResponse:
    """模拟站点链消费的最小 HTTP 响应。"""

    def __init__(
        self,
        status_code: int,
        *,
        text: str = "",
        content: bytes = b"",
        reason: str = "OK",
        payload: Optional[dict] = None,
    ) -> None:
        """保存响应状态、正文与可选 JSON。"""
        self.status_code = status_code
        self.text = text
        self.content = content
        self.reason = reason
        self._payload = payload or {}

    def __bool__(self) -> bool:
        """模拟 requests.Response 的成功状态真假语义。"""
        return self.status_code < 400

    def json(self) -> dict:
        """返回预设 JSON 数据。"""
        return self._payload


class _FakeHttpPort:
    """按队列回放响应并记录上下文退出。"""

    def __init__(self, *responses: Optional[_FakeResponse]) -> None:
        """保存将被依次消费的响应。"""
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = 0

    @contextmanager
    def open(self, **kwargs) -> Iterator[Optional[_FakeResponse]]:
        """回放一个响应并在调用方离开时记录关闭。"""
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        try:
            yield response
        finally:
            self.closed += 1


class _FakeBrowserPort:
    """回放浏览器页面源码。"""

    def __init__(self, source: Optional[str] = None) -> None:
        """保存渲染结果。"""
        self.source = source
        self.calls: list[dict] = []

    def render(self, **kwargs) -> Optional[str]:
        """记录调用并返回页面源码。"""
        self.calls.append(kwargs)
        return self.source


class _FakeChallengePort:
    """回放页面挑战识别结果。"""

    def __init__(self, detected: bool = False) -> None:
        """保存识别结果。"""
        self.result = detected
        self.calls: list[str] = []

    def detected(self, html_text: str) -> bool:
        """记录页面并返回识别结果。"""
        self.calls.append(html_text)
        return self.result


class _FakeCookieCloudPort:
    """回放 CookieCloud 下载二元结果。"""

    def __init__(self, result=(None, "未配置")) -> None:
        """保存下载结果。"""
        self.result = result
        self.calls = 0

    def download(self):
        """记录调用并返回预设结果。"""
        self.calls += 1
        return self.result


@pytest.fixture(autouse=True)
def clean_site_ports():
    """隔离每个用例修改的站点链进程级端口。"""
    reset_site_ports()
    yield
    reset_site_ports()


def _configure(
    http: _FakeHttpPort,
    *,
    browser: Optional[_FakeBrowserPort] = None,
    challenge: Optional[_FakeChallengePort] = None,
    cookiecloud: Optional[_FakeCookieCloudPort] = None,
) -> None:
    """装配一组完全离线的站点链假端口。"""
    configure_site_ports(
        http=http,
        browser=browser or _FakeBrowserPort(),
        challenge=challenge or _FakeChallengePort(),
        cookiecloud=cookiecloud or _FakeCookieCloudPort(),
    )


def _chain() -> SiteChain:
    """构造仅执行私有连通性逻辑的 SiteChain。"""
    chain = object.__new__(SiteChain)
    chain.runtime_config = SimpleNamespace(
        proxy=None,
        proxy_host=None,
        proxy_server=None,
        user_agent="MoviePilot-Test",
    )
    return chain


def _public_site(*, render: bool = False) -> SiteSnapshot:
    """构造无需登录标志判断的站点快照。"""
    return SiteSnapshot(
        id=1,
        name="Port Test",
        domain="port.test",
        url="https://port.test/",
        public=True,
        render=render,
    )


def test_unconfigured_site_port_fails_without_implicit_adapter() -> None:
    """未装配时真实访问应稳定失败，不能由 Chain 隐式回建 Adapter。"""
    with pytest.raises(RuntimeError, match="站点链访问端口"):
        _chain()._SiteChain__test(_public_site())


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, (False, "无法打开网站！")),
        (_FakeResponse(429, reason="rate"), (False, "错误：429 rate！")),
        (_FakeResponse(200, text="ok"), (True, "连接成功")),
    ],
)
def test_http_port_preserves_failure_tristate_and_closes_response(
    response: Optional[_FakeResponse], expected: tuple[bool, str]
) -> None:
    """无响应、错误响应与成功响应保持原三态，且所有分支退出上下文。"""
    http = _FakeHttpPort(response)
    _configure(http)

    result = _chain()._SiteChain__test(_public_site())

    assert result == expected
    assert http.closed == 1
    assert http.calls[0]["url"] == "https://port.test/"


def test_browser_and_challenge_ports_keep_render_failure_semantics() -> None:
    """浏览器页面命中挑战时保留原 Cloudflare 失败提示。"""
    browser = _FakeBrowserPort("<title>Just a moment...</title>")
    challenge = _FakeChallengePort(True)
    _configure(_FakeHttpPort(), browser=browser, challenge=challenge)
    site = SiteSnapshot(
        id=2,
        name="Protected",
        domain="protected.test",
        url="https://protected.test/",
        public=False,
        render=True,
    )

    result = _chain()._SiteChain__test(site)

    assert result == (False, "无法通过Cloudflare！")
    assert challenge.calls == ["<title>Just a moment...</title>"]
    assert browser.calls[0]["url"] == "https://protected.test/"


def test_cookiecloud_failure_uses_fake_port_without_network() -> None:
    """CookieCloud 下载失败应保持二元返回且只调用注入端口。"""
    cookiecloud = _FakeCookieCloudPort((None, "下载失败"))
    _configure(_FakeHttpPort(), cookiecloud=cookiecloud)

    result = _chain().sync_cookies()

    assert result == (False, "下载失败")
    assert cookiecloud.calls == 1


def test_configure_reset_is_repeatable_and_preserves_chain_identity() -> None:
    """重复装配与 reset 必须原子替换快照且不改变 SiteChain 类身份。"""
    original_type = SiteChain
    first = _FakeHttpPort(_FakeResponse(200))
    second = _FakeHttpPort(_FakeResponse(200))

    _configure(first)
    first_snapshot = site_module._site_ports_snapshot()
    _configure(second)
    second_snapshot = site_module._site_ports_snapshot()
    reset_site_ports()
    reset_site_ports()

    assert first_snapshot.http is first
    assert second_snapshot.http is second
    assert first_snapshot is not second_snapshot
    assert site_module.SiteChain is original_type
    with pytest.raises(RuntimeError, match="站点链访问端口"):
        site_module._site_ports_snapshot()


def test_initializer_rolls_back_all_ports_when_site_chain_publish_fails(
    monkeypatch,
) -> None:
    """最后一组站点链端口发布失败时必须回滚此前三组端口。"""

    def fail_site_configuration(**_kwargs) -> None:
        """模拟站点链端口发布失败。"""
        raise RuntimeError("site publish failed")

    monkeypatch.setattr(
        site_composition, "configure_site_ports", fail_site_configuration
    )

    with pytest.raises(RuntimeError, match="site publish failed"):
        site_initializer.init_site_access_ports()

    with pytest.raises(RuntimeError):
        rss_module._require_rss_ports()
    with pytest.raises(RuntimeError):
        cookie_module._require_cookie_ports()
    with pytest.raises(RuntimeError):
        torrent_module._require_torrent_port()
    with pytest.raises(RuntimeError):
        site_module._site_ports_snapshot()


def test_real_initializer_http_adapter_closes_response(monkeypatch) -> None:
    """组合根 HTTP adapter 必须在调用方返回后关闭 RequestUtils 响应。"""
    response = _FakeResponse(200, text="ok")
    state = {"closed": False}

    class FakeRequest:
        """提供可观测关闭的 RequestUtils 替身。"""

        def __init__(self, **_kwargs) -> None:
            """接受真实 adapter 传递的请求配置。"""

        @contextmanager
        def response_manager(self, **_kwargs):
            """退出上下文时记录响应已释放。"""
            try:
                yield response
            finally:
                state["closed"] = True

    monkeypatch.setattr(site_composition, "RequestUtils", FakeRequest)
    adapter = site_composition._SiteHttpAdapter()

    with adapter.open(method="GET", url="https://close.test/") as opened:
        assert opened is response
        assert not state["closed"]

    assert state["closed"]


def test_cookiecloud_adapter_closes_remote_response(monkeypatch) -> None:
    """CookieCloud 远端下载完成或提前返回时也必须关闭 HTTP 响应。"""
    state = {"closed": False}
    response = _FakeResponse(200, payload={"encrypted": "cipher"})

    class FakeRequest:
        """提供可观测关闭的 CookieCloud HTTP 替身。"""

        def __init__(self, **_kwargs) -> None:
            """接受 CookieCloud 传递的内容类型。"""

        @contextmanager
        def response_manager(self, **_kwargs):
            """退出下载上下文时记录响应已关闭。"""
            try:
                yield response
            finally:
                state["closed"] = True

    helper = cookiecloud_module.CookieCloudHelper.__new__(
        cookiecloud_module.CookieCloudHelper
    )
    helper._server = "https://cookiecloud.test/"
    helper._key = "key"
    helper._password = "password"
    helper._enable_local = False
    monkeypatch.setattr(helper, "_CookieCloudHelper__sync_setting", lambda: None)
    monkeypatch.setattr(cookiecloud_module, "RequestUtils", FakeRequest)
    monkeypatch.setattr(
        cookiecloud_module.CryptoJsUtils,
        "decrypt",
        lambda *_args: json.dumps(
            {
                "cookie_data": {
                    "source": [
                        {"domain": ".example.test", "name": "sid", "value": "1"}
                    ]
                }
            }
        ).encode(),
    )

    cookies, message = helper.download()

    assert cookies == {"example.test": "sid=1"}
    assert message == ""
    assert state["closed"]


def test_site_chain_has_no_concrete_adapter_imports() -> None:
    """SiteChain 只能拥有 Port，不得重新静态依赖具体 Adapter。"""
    path = Path(site_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("app.adapters")
    ]

    assert violations == []
