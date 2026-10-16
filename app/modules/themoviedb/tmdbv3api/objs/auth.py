from ..exceptions import TMDbException
from ..tmdb import TMDb, TMDbJson


class Authentication(TMDb):
    """按令牌、账号验证、会话创建顺序执行 TMDb 认证。"""

    _urls = {
        "create_request_token": "/authentication/token/new",
        "validate_with_login": "/authentication/token/validate_with_login",
        "create_session": "/authentication/session/new",
        "delete_session": "/authentication/session",
    }

    def __init__(self, username: str, password: str) -> None:
        """保存凭据并完成认证，不吞掉上游业务错误。"""
        super().__init__()
        self.username = username
        self.password = password
        self.expires_at: TMDbJson = None
        self.request_token = self._create_request_token()
        self._authorise_request_token_with_login()
        self._create_session()

    def _create_request_token(self) -> str:
        """
        Create a temporary request token that can be used to validate a TMDb user login.
        """
        response = self._request_obj(self._urls["create_request_token"])
        self.expires_at = self._get_json_key(response, "expires_at")
        return self._required_string(response, "request_token")

    def _create_session(self) -> None:
        """
        You can use this method to create a fully valid session ID once a user has validated the request token.
        """
        response = self._request_obj(
            self._urls["create_session"],
            method="POST",
            json={"request_token": self.request_token}
        )
        self.session_id = self._required_string(response, "session_id")

    def _authorise_request_token_with_login(self) -> None:
        """
        This method allows an application to validate a request token by entering a username and password.
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
        If you would like to delete (or "logout") from a session, call this method with a valid session ID.
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
        """验证认证必需字段，避免把无效响应继续作为凭据发送。"""
        value = cls._get_json_key(response, field)
        if not isinstance(value, str) or not value:
            raise TMDbException(f"TheMovieDb 认证响应缺少有效字段 {field!r}")
        return value
