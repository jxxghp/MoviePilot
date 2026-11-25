"""
MusicBrainz HTTP 响应替身；不注册测试用例。
"""


class _FakeMusicBrainzResponse:
    """
    模拟 MusicBrainz HTTP 响应，便于缓存回归测试统计网络调用次数。
    """

    def __init__(self, payload, status_code=200):
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param payload: 外部响应的模拟正文
        :param status_code: 模拟的 HTTP 状态码
        """
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        """
        返回预设的 JSON 负载。
        """
        return self._payload

    def __bool__(self):
        """
        模拟 requests.Response：HTTP 错误状态在布尔判断中为 False。
        """
        return self.status_code < 400

    def close(self):
        """
        无需释放的资源。
        """
