"""
TMDB 账号认证接口的同步与异步调用封装。
"""

from ..exceptions import TMDbException
from ..tmdb import TMDb, TMDbJson


class Authentication(TMDb):
    """
    按令牌、账号验证、会话创建顺序执行 TMDb 认证。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "create_request_token": "/authentication/token/new",
        "validate_with_login": "/authentication/token/validate_with_login",
        "create_session": "/authentication/session/new",
        "delete_session": "/authentication/session",
    }

    def __init__(self, username: str, password: str) -> None:
        """
        保存账号密码并依次申请临时令牌、验证登录和创建会话。

        构造过程会发起网络请求并写入会话配置；认证失败向调用方抛出异常。

        :param username: TMDB 账号用户名
        :param password: TMDB 账号密码
        """
        super().__init__()
        self.username = username
        self.password = password
        self.expires_at: TMDbJson = None
        self.request_token = self._create_request_token()
        self._authorise_request_token_with_login()
        self._create_session()

    def _create_request_token(self) -> str:
        """
        申请临时登录令牌并保存其过期时间。

        :return: 非空临时令牌字符串
        """
        response = self._request_obj(self._urls["create_request_token"])
        self.expires_at = self._get_json_key(response, "expires_at")
        return self._required_string(response, "request_token")

    def _create_session(self) -> None:
        """
        以验证后的临时令牌创建会话，并保存会话标识。
        """
        response = self._request_obj(
            self._urls["create_session"],
            method="POST",
            json={"request_token": self.request_token}
        )
        self.session_id = self._required_string(response, "session_id")

    def _authorise_request_token_with_login(self) -> None:
        """
        使用账号密码验证当前临时令牌。
        """
        self._request_obj(
            self._urls["validate_with_login"],
            method="POST",
            json={
                "username": self.username,
                "password": self.password,
                "request_token": self.request_token,
            }
        )

    def delete_session(self) -> None:
        """
        删除已有会话，成功返回后清空本地会话标识。
        """
        if self.has_session:
            self._request_obj(
                self._urls["delete_session"],
                method="DELETE",
                json={"session_id": self.session_id}
            )
            self.session_id = ""

    @classmethod
    def _required_string(cls, response: TMDbJson, field: str) -> str:
        """
        验证认证必需字段，避免把无效响应继续作为凭据发送。
        """
        value = cls._get_json_key(response, field)
        if not isinstance(value, str) or not value:
            raise TMDbException(f"TheMovieDb 认证响应缺少有效字段 {field!r}")
        return value
