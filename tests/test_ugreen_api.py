from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from app.modules.ugreen.api import Api


class _FakeResponse:
    def __init__(self, payload: dict, headers: Optional[dict] = None) -> None:
        """初始化伪造 HTTP 响应"""
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict:
        """返回伪造 JSON 响应体"""
        return self._payload


class _FakeSession:
    def __init__(
        self,
        get_responses: Optional[list[_FakeResponse]] = None,
        post_responses: Optional[list[_FakeResponse]] = None,
    ) -> None:
        """初始化伪造 HTTP 会话"""
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
        self.calls: list[tuple[str, dict]] = []
        self.cookies = SimpleNamespace(
            get_dict=lambda: {},
            update=lambda *_args, **_kwargs: None,
        )

    def get(self, *args: object, **kwargs: object) -> _FakeResponse:
        """记录 GET 请求并返回预置响应"""
        if args:
            kwargs = {"url": args[0], **kwargs}
        self.calls.append(("GET", kwargs))
        return self._get_responses.pop(0) if self._get_responses else _FakeResponse({})

    def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        """记录 POST 请求并返回预置响应"""
        if args:
            kwargs = {"url": args[0], **kwargs}
        self.calls.append(("POST", kwargs))
        return self._post_responses.pop(0) if self._post_responses else _FakeResponse({})

    @staticmethod
    def close() -> None:
        """关闭伪造会话"""
        return None


class _FakeCrypto:
    def __init__(self, *args: object, **kwargs: object) -> None:
        """初始化伪造加密工具"""
        return None

    @staticmethod
    def rsa_encrypt_long(raw: str) -> str:
        """返回可断言的伪造 RSA 加密内容"""
        return f"enc:{raw}"

    @staticmethod
    def build_encrypted_request(
        url: str,
        method: str = "GET",
        params: Optional[dict] = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        """构造伪造加密请求对象"""
        _ = method, kwargs
        return SimpleNamespace(
            url=url,
            headers={},
            params=params or {},
            json=None,
            aes_key="k",
        )

    @staticmethod
    def decrypt_response(payload: dict, aes_key: str) -> dict:
        """原样返回伪造响应内容"""
        _ = aes_key
        return payload


def test_request_json_default_verify_ssl_true() -> None:
    """默认请求应开启 HTTPS 证书校验"""
    api = Api(host="https://example.com")
    fake_session = _FakeSession(
        get_responses=[_FakeResponse({"code": 200})],
        post_responses=[_FakeResponse({"code": 200})],
    )
    api._session = fake_session

    api._request_json(url="https://example.com/a", method="GET")
    api._request_json(url="https://example.com/b", method="POST", json_data={"x": 1})

    assert fake_session.calls[0][1].get("verify") is True
    assert fake_session.calls[1][1].get("verify") is True


def test_login_logout_requests_follow_client_configuration() -> None:
    """登录与登出请求应沿用证书配置并携带一致的客户端标识"""
    api = Api(host="https://example.com", verify_ssl=False)
    fake_session = _FakeSession(
        get_responses=[_FakeResponse({})],
        post_responses=[
            _FakeResponse(
                {"code": 200, "msg": "ok", "data": {}},
                headers={"x-rsa-token": "BEGIN CHECK KEY"},
            ),
            _FakeResponse(
                {
                    "code": 200,
                    "msg": "ok",
                    "data": {
                        "token": "token-value",
                        "public_key": "BEGIN LOGIN KEY",
                        "static_token": "static-token",
                        "is_ugk": False,
                    },
                }
            ),
        ],
    )
    api._session = fake_session

    with patch("app.modules.ugreen.api.UgreenCrypto", _FakeCrypto):
        token = api.login("tester", "pwd")
        assert token == "token-value"
        assert api.public_key == "BEGIN LOGIN KEY"
        assert api.static_token == "static-token"
        api.logout()

    assert len(fake_session.calls) == 3
    assert fake_session.calls[0][0] == "POST"
    assert fake_session.calls[1][0] == "POST"
    assert fake_session.calls[2][0] == "GET"
    assert fake_session.calls[0][1].get("verify") is False
    assert fake_session.calls[1][1].get("verify") is False
    assert fake_session.calls[2][1].get("verify") is False
    check_headers = fake_session.calls[0][1]["headers"]
    login_headers = fake_session.calls[1][1]["headers"]
    assert check_headers["UG-Client-Id"] == check_headers["Client-Id"]
    assert login_headers["UG-Client-Id"] == check_headers["UG-Client-Id"]


def test_login_accepts_token_id_and_reuses_check_public_key() -> None:
    """登录响应只有 token_id 时应复用检查接口公钥完成会话初始化"""
    api = Api(host="https://example.com")
    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(
                {"code": 200, "msg": "ok", "data": {}},
                headers={"x-rsa-token": "BEGIN CHECK KEY"},
            ),
            _FakeResponse(
                {
                    "code": 200,
                    "msg": "success",
                    "data": {
                        "enable_otp": True,
                        "is_exceed": False,
                        "role": "admin",
                        "token_id": "token-id-value",
                        "uid": 1000,
                        "urgent_email": "tester@example.com",
                    },
                }
            ),
        ],
    )
    api._session = fake_session

    with patch("app.modules.ugreen.api.UgreenCrypto", _FakeCrypto):
        token = api.login("tester", "pwd")

    assert token == "token-id-value"
    assert api.token == "token-id-value"
    assert api.static_token == "token-id-value"
    assert api.public_key == "BEGIN CHECK KEY"
