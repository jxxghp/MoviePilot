"""
TMDB 缓存协议的 HTTP 响应替身；不注册测试用例。
"""

from threading import RLock


class _FakeResponse:
    """
    测试用响应对象，模拟 requests/httpx 响应的最小接口。
    """

    def __init__(self, payload, headers: dict, status_code: int = 200, text: str = ""):
        """
        初始化响应内容。
        """
        self._payload = payload
        self.headers = headers
        self.status_code = status_code
        self.text = text
        self._lock = RLock()

    def json(self):
        """
        返回预置JSON内容。
        """
        return self._payload
