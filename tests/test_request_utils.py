import requests

from app.adapters.network import http as http_module
from app.adapters.network.http import AsyncRequestUtils, RequestUtils


class _FakeSession:
    """
    测试用 requests.Session 替身，记录请求次数与连接池关闭行为。
    """

    def __init__(self, side_effects):
        """
        初始化请求结果序列。

        :param side_effects: 每次 request 调用要返回或抛出的对象
        """
        self.side_effects = list(side_effects)
        self.calls = []
        self.close_count = 0
        self.cookies = _FakeCookies()

    def request(self, method, url, **kwargs):
        """
        模拟 requests.Session.request。
        """
        self.calls.append((method, url, kwargs))
        effect = self.side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def close(self):
        """
        模拟清空 session 连接池。
        """
        self.close_count += 1


class _FakeCookies:
    """测试用 Cookie jar，仅暴露 RequestUtils 所需能力。"""

    def __init__(self) -> None:
        """初始化空 Cookie 集合。"""
        self._values = {}

    def update(self, values: dict) -> None:
        """合并 Cookie。"""
        self._values.update(values)

    def get_dict(self) -> dict:
        """返回 Cookie 独立快照。"""
        return dict(self._values)


def _make_response(status_code: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    return response


def test_configured_user_agent_marks_plugin_requests(monkeypatch):
    """启动层注入宿主 UA 后，同步和异步客户端仍应标记插件调用来源。"""
    monkeypatch.setattr(http_module, "_default_user_agent", None)
    monkeypatch.setattr(http_module, "get_caller", lambda: "DemoPlugin")
    http_module.configure_default_user_agent("MoviePilot-Test")

    request_utils = RequestUtils(ua="MoviePilot-Test")
    async_request_utils = AsyncRequestUtils(ua="MoviePilot-Test")

    assert request_utils._headers["User-Agent"] == "MoviePilot-Test Plugin/DemoPlugin"
    assert async_request_utils._headers["User-Agent"] == "MoviePilot-Test Plugin/DemoPlugin"


def test_request_utils_retries_idempotent_session_connection_error():
    """
    同步幂等请求遇到失效 session 连接时应清理连接池并重试一次。
    """
    response = _make_response()
    session = _FakeSession(
        [
            requests.exceptions.ConnectionError("stale keep-alive"),
            response,
        ]
    )
    request_utils = RequestUtils(session=session)

    result = request_utils.get_res("https://example.com/data")

    assert result is response
    assert len(session.calls) == 2
    assert session.close_count == 1


def test_request_utils_does_not_retry_non_idempotent_connection_error():
    """
    非幂等请求连接异常时不应自动重试，避免重复提交副作用。
    """
    session = _FakeSession(
        [
            requests.exceptions.ConnectionError("connection failed"),
            _make_response(),
        ]
    )
    request_utils = RequestUtils(session=session)

    result = request_utils.post_res("https://example.com/data", data={"name": "demo"})

    assert result is None
    assert len(session.calls) == 1
    assert session.close_count == 0


def test_request_utils_raises_retry_error_when_retry_still_fails():
    """
    开启 raise_exception 后，重试仍失败时应抛出重试阶段的异常。
    """
    first_error = requests.exceptions.ConnectionError("stale keep-alive")
    retry_error = requests.exceptions.ConnectionError("proxy still unavailable")
    session = _FakeSession([first_error, retry_error])
    request_utils = RequestUtils(session=session)

    try:
        request_utils.get_res("https://example.com/data", raise_exception=True)
    except requests.exceptions.ConnectionError as err:
        assert err is retry_error
    else:
        raise AssertionError("请求重试失败时应抛出异常")

    assert len(session.calls) == 2
    assert session.close_count == 1


def test_request_utils_owns_persistent_headers_cookies_and_close(monkeypatch):
    """宿主持久客户端应封装 Session 状态，并只暴露规范状态操作。"""
    response = _make_response()
    session = _FakeSession([response])
    monkeypatch.setattr(http_module.requests, "Session", lambda: session)

    request_utils = RequestUtils(use_session=True, headers={"Accept": "*/*"})
    request_utils.update_headers({"Authorization": "Bearer token"})
    request_utils.update_cookies({"sid": "cookie-value"})
    result = request_utils.get_res("https://example.com/data")

    assert result is response
    assert session.calls[0][2]["headers"] == {
        "Accept": "*/*",
        "Authorization": "Bearer token",
    }
    assert request_utils.get_cookies() == {"sid": "cookie-value"}

    request_utils.close()
    assert session.close_count == 1


def test_request_utils_does_not_close_compat_injected_session() -> None:
    """旧 session= 兼容入口的生命周期仍由调用方管理。"""
    session = _FakeSession([])

    RequestUtils(session=session).close()

    assert session.close_count == 0


def test_request_utils_preserves_default_and_explicit_tls_verification() -> None:
    """同步统一客户端默认保持兼容值，迁移调用方可显式启用证书校验。"""
    default_session = _FakeSession([_make_response()])
    secure_session = _FakeSession([_make_response()])

    RequestUtils(session=default_session).get_res("https://example.com/default")
    RequestUtils(session=secure_session, verify=True).get_res(
        "https://example.com/secure"
    )

    assert default_session.calls[0][2]["verify"] is False
    assert secure_session.calls[0][2]["verify"] is True


def test_request_utils_normalizes_per_request_cookie_string(monkeypatch) -> None:
    """单次请求传入 Cookie 字符串时应先转换为 requests 支持的字典。"""
    response = _make_response()
    session = requests.Session()
    prepared_requests = []

    def fake_send(request, **_kwargs):
        prepared_requests.append(request)
        return response

    monkeypatch.setattr(session, "send", fake_send)

    result = RequestUtils(session=session).get_res(
        "https://example.com/data",
        cookies="sid=1; token=two",
    )

    assert result is response
    assert prepared_requests[0].headers["Cookie"] == "sid=1; token=two"
