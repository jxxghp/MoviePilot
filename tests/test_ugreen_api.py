import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.ugreen.api import Api


class _FakeResponse:
    def __init__(self, payload: dict, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
        self.calls: list[tuple[str, dict]] = []
        self.cookies = SimpleNamespace(
            get_dict=lambda: {},
            update=lambda *_args, **_kwargs: None,
        )

    def get(self, *args, **kwargs):
        if args:
            kwargs = {"url": args[0], **kwargs}
        self.calls.append(("GET", kwargs))
        return self._get_responses.pop(0) if self._get_responses else _FakeResponse({})

    def post(self, *args, **kwargs):
        if args:
            kwargs = {"url": args[0], **kwargs}
        self.calls.append(("POST", kwargs))
        return self._post_responses.pop(0) if self._post_responses else _FakeResponse({})

    @staticmethod
    def close():
        return None


class _FakeCrypto:
    def __init__(self, *args, **kwargs):
        pass

    @staticmethod
    def rsa_encrypt_long(raw: str) -> str:
        return f"enc:{raw}"

    @staticmethod
    def build_encrypted_request(url: str, method: str = "GET", params=None, **kwargs):
        return SimpleNamespace(url=url, headers={}, params=params or {}, json=None, aes_key="k")

    @staticmethod
    def decrypt_response(payload, aes_key):
        return payload


class UgreenApiVerifySslTest(unittest.TestCase):
    def test_request_json_default_verify_ssl_true(self):
        api = Api(host="https://example.com")
        fake_session = _FakeSession(
            get_responses=[_FakeResponse({"code": 200})],
            post_responses=[_FakeResponse({"code": 200})],
        )
        api._session = fake_session

        api._request_json(url="https://example.com/a", method="GET")
        api._request_json(url="https://example.com/b", method="POST", json_data={"x": 1})

        self.assertEqual(fake_session.calls[0][1].get("verify"), True)
        self.assertEqual(fake_session.calls[1][1].get("verify"), True)

    def test_login_logout_follow_verify_ssl_flag(self):
        api = Api(host="https://example.com", verify_ssl=False)
        fake_session = _FakeSession(
            get_responses=[_FakeResponse({})],
            post_responses=[
                _FakeResponse({"code": 200, "msg": "ok", "data": {}}, headers={"x-rsa-token": "BEGIN TEST"}),
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
            self.assertEqual(token, "token-value")
            api.logout()

        self.assertEqual(len(fake_session.calls), 3)
        self.assertEqual(fake_session.calls[0][0], "POST")
        self.assertEqual(fake_session.calls[1][0], "POST")
        self.assertEqual(fake_session.calls[2][0], "GET")
        self.assertEqual(fake_session.calls[0][1].get("verify"), False)
        self.assertEqual(fake_session.calls[1][1].get("verify"), False)
        self.assertEqual(fake_session.calls[2][1].get("verify"), False)
