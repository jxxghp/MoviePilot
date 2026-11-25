"""
声学指纹响应回放与记录标识；不注册测试用例。
"""


RECORDING_ID = "38035858-f990-4fbb-b3b2-f2f8b958eeba"


class FakeResponse:
    """
    提供 AcoustID 模块测试所需的最小 HTTP 响应接口。
    """

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        """
        保存响应数据并记录资源是否被关闭。
        """
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self) -> dict:
        """
        返回预设 JSON 响应。
        """
        return self.payload

    def close(self) -> None:
        """
        记录响应资源已释放。
        """
        self.closed = True
