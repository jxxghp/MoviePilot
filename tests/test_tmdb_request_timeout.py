# -*- coding: utf-8 -*-
"""
覆盖 TMDB 请求客户端超时契约的回归测试。

上游引入 `TMDb._timeout = 15` 时把它显式传给了当时的两处请求调用；后续将传输统一
改走 RequestUtils/AsyncRequestUtils 的重构丢掉了这两个 `timeout=` 实参，并把赋值
留在了同步与异步客户端构造之后，使声明的 15 秒静默回退成 20 秒默认值。识别链因此
按更长的时间等待抖动链路，连接失败退避重试进一步放大了占用。
"""
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb


class _RecordingSession:
    """
    记录传输层实际收到的请求参数，用于断言超时是否真正下发。
    """

    def __init__(self, payload: dict):
        """
        保存预置响应载荷并初始化调用记录。
        """
        self._payload = payload
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        """
        记录本次请求参数并返回预置响应。
        """
        self.calls.append(kwargs)
        return _FakeResponse(payload=self._payload)

    def close(self) -> None:
        """
        兼容 requests.Session 的关闭接口，测试会话无需释放真实资源。
        """


class _FakeResponse:
    """
    测试用响应对象，模拟 requests 响应的最小接口。
    """

    def __init__(self, payload: dict, headers: dict = None):
        """
        初始化响应内容。
        """
        self._payload = payload
        self.headers = headers or {}
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        """
        返回预置JSON内容。
        """
        return self._payload


def test_sync_transport_receives_declared_timeout():
    """
    同步请求必须把声明的 15 秒超时下发到传输层，而不是回退到 RequestUtils 默认的 20 秒。
    """
    session = _RecordingSession(payload={"id": 1})
    tmdb = TMDb(session=session)

    result = TMDb.request.__wrapped__(tmdb, "GET", "https://example.com/3/configuration", None, None)

    assert result["json"] == {"id": 1}
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 15


def test_async_transport_receives_declared_timeout():
    """
    异步请求客户端与同步客户端共用同一超时声明，避免只有其中一条链路受约束。
    """
    tmdb = TMDb()

    assert tmdb._async_req._timeout == 15


def test_reset_owned_session_keeps_declared_timeout():
    """
    连接失败后重建自有会话时，超时声明必须保留，否则重试路径会退回默认值。
    """
    tmdb = TMDb()
    assert tmdb._req._timeout == 15

    tmdb._reset_owned_session()

    assert tmdb._req._timeout == 15
