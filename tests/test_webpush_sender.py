"""
Web Push 协议发送测试：用模拟浏览器订阅解密投递内容，并校验 VAPID 签名与请求头。
"""
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import http_ece
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from app.adapters.network.webpush import WebPushDeliveryError, configure_webpush_endpoint_validator, send_webpush
from app.application.security.url import configure_dns_resolver, reset_dns_resolver


@pytest.fixture(autouse=True)
def public_push_dns():
    """
    将公网策略的 DNS 端口装配为离线地址，退出时恢复解析器和缓存。

    :yields: 可由用例调整返回地址的独立解析器
    """
    from app.startup.composition.webpush import validate_webpush_origin

    resolver = SimpleNamespace(resolve=lambda _hostname: ["8.8.8.8"])
    previous = configure_dns_resolver(resolver)
    previous_validator = configure_webpush_endpoint_validator(validate_webpush_origin)
    try:
        yield resolver
    finally:
        reset_dns_resolver(previous)
        configure_webpush_endpoint_validator(previous_validator)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_webpush_rejects_malformed_origin_before_transport(browser, vapid):
    """
    无效的 HTTPS 源站不会到达传输层，也不会在错误中暴露端点路径。
    """
    browser.subscription["endpoint"] = "https://[invalid/send/private-token"
    with patch("app.adapters.network.webpush.RequestUtils") as client:
        with pytest.raises(WebPushDeliveryError, match="完整 HTTPS 地址") as exc:
            send_webpush(browser.subscription, "{}", vapid.private_key, "mailto:push@example.test")
    client.assert_not_called()
    assert "private-token" not in str(exc.value)


@pytest.fixture
def browser():
    """
    模拟浏览器 PushSubscription：接收方 P-256 密钥对与 16 字节 auth 密钥。
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    subscription = {
        "endpoint": "https://push.example.com/send/abc?x=1",
        "keys": {"p256dh": _b64url(public_raw), "auth": _b64url(auth)},
    }
    return SimpleNamespace(private_key=private_key, auth=auth, subscription=subscription)


@pytest.fixture
def vapid():
    """
    生成与宿主 VAPID 配置同格式（base64url 原始私钥）的服务端密钥。
    """
    key = Vapid()
    key.generate_keys()
    raw = key.private_key.private_numbers().private_value.to_bytes(32, "big")
    return SimpleNamespace(private_key=_b64url(raw), public_key=key.public_key)


def _post(status_code=201):
    """
    拦截 RequestUtils 构造与 POST，记录请求头和正文。
    """
    captured = {}

    class _Utils:
        """
        捕获协议发送器的 HTTP 参数，不创建网络连接。
        """

        def __init__(self, headers=None, timeout=None, verify=None, **_):
            """
            保存构造时的 TLS、超时和请求头配置。

            :param headers: Web Push 请求头
            :param timeout: 本次请求超时
            :param verify: 是否验证 TLS
            :param _: 不参与本用例的其它构造参数
            """
            captured.update(headers=headers, timeout=timeout, verify=verify)

        def post_res(self, url, data=None, allow_redirects=True, **_):
            """
            记录推送地址与加密请求体，并返回指定 HTTP 状态的离线响应。
            """
            captured.update(url=url, body=data, allow_redirects=allow_redirects)
            return SimpleNamespace(status_code=status_code, reason="R", text="detail")

    return captured, patch("app.adapters.network.webpush.RequestUtils", _Utils)


def test_payload_decrypts_with_browser_keys_and_vapid_verifies(browser, vapid):
    """
    浏览器私钥能解出原文；VAPID JWT 由服务端公钥签名，aud 为端点源站。
    """
    captured, patcher = _post()
    payload = json.dumps({"title": "标题", "body": "内容"}, ensure_ascii=False)
    with patcher:
        send_webpush(browser.subscription, payload, vapid.private_key, "mailto:admin@example.com")

    decrypted = http_ece.decrypt(
        captured["body"],
        private_key=browser.private_key,
        auth_secret=browser.auth,
        version="aes128gcm",
    )
    assert decrypted.decode("utf-8") == payload

    headers = captured["headers"]
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["TTL"] == "0"
    assert captured["url"] == browser.subscription["endpoint"]
    assert captured["verify"] is True
    assert captured["allow_redirects"] is False

    scheme, _, params = headers["Authorization"].partition(" ")
    assert scheme == "vapid"
    fields = dict(item.strip().split("=", 1) for item in params.split(","))
    public_pem = vapid.public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    claims = jwt.decode(fields["t"], public_pem, algorithms=["ES256"], audience="https://push.example.com")
    assert claims["sub"] == "mailto:admin@example.com"
    assert time.time() < claims["exp"] <= time.time() + 24 * 3600
    assert base64.urlsafe_b64decode(fields["k"] + "==") == vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def test_each_send_uses_fresh_ephemeral_key(browser, vapid):
    """
    同一内容两次加密结果不同（盐与临时 ECDH 公钥都随机），且都能解密。
    """
    captured, patcher = _post()
    bodies = []
    with patcher:
        for _ in range(2):
            send_webpush(browser.subscription, "same", vapid.private_key, "mailto:a@b.c")
            bodies.append(captured["body"])
    assert bodies[0] != bodies[1]
    for body in bodies:
        assert http_ece.decrypt(body, private_key=browser.private_key,
                                auth_secret=browser.auth, version="aes128gcm") == b"same"


def test_endpoint_headers_and_ttl_are_forwarded(browser, vapid):
    """
    WNS 等端点的附加请求头和 TTL 原样发送，VAPID 头不被调用方覆盖。
    """
    captured, patcher = _post()
    with patcher:
        send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c",
                     ttl=86400, headers={"X-WNS-Cache-Policy": "cache", "Authorization": "fake"})
    assert captured["headers"]["X-WNS-Cache-Policy"] == "cache"
    assert captured["headers"]["TTL"] == "86400"
    assert captured["headers"]["Authorization"].startswith("vapid t=")


@pytest.mark.parametrize("status_code", [404, 410, 413])
def test_rejected_delivery_raises_with_response(browser, vapid, status_code):
    """
    推送服务返回 202 以上状态码时抛错并带上响应，供调用方清理失效订阅。
    """
    _, patcher = _post(status_code)
    with patcher, pytest.raises(WebPushDeliveryError) as exc:
        send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c")
    assert exc.value.response.status_code == status_code


@pytest.mark.parametrize("keys", [{}, {"p256dh": "AAAA", "auth": "AAAA"}])
def test_invalid_subscription_keys_are_rejected_before_request(vapid, keys):
    """
    缺少或格式错误的订阅密钥在发请求前报错，且不带响应（不会被当成失效订阅删除）。
    """
    captured, patcher = _post()
    with patcher, pytest.raises(WebPushDeliveryError) as exc:
        send_webpush({"endpoint": "https://push.example.com/x", "keys": keys}, "x",
                     vapid.private_key, "mailto:a@b.c")
    assert exc.value.response is None
    assert "url" not in captured


def test_no_response_is_reported(browser, vapid):
    """
    网络失败（RequestUtils 返回 None）时抛错而不是静默成功。
    """
    with patch("app.adapters.network.webpush.RequestUtils") as utils:
        utils.return_value.post_res.return_value = None
        with pytest.raises(WebPushDeliveryError):
            send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c")


def test_host_configured_vapid_key_signs(browser, vapid, monkeypatch):
    """
    运行期注入的默认 VAPID 密钥对可以签出有效的 Authorization 头，且宿主源码不内置明文密钥。
    """
    from app.runtime.config import settings
    from app.runtime.settings import get_runtime_setting

    public_b64 = _b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "VAPID_SUBJECT", None)
    monkeypatch.setattr(settings, "_runtime_defaults", {})

    empty_vapid = get_runtime_setting("VAPID")
    assert empty_vapid["publicKey"] == ""
    assert empty_vapid["privateKey"] == ""

    monkeypatch.setattr(
        settings,
        "_runtime_defaults",
        {
            "VAPID_PUBLIC_KEY": public_b64,
            "VAPID_PRIVATE_KEY": vapid.private_key,
        },
    )
    vapid_config = get_runtime_setting("VAPID")
    captured, patcher = _post()
    with patcher:
        send_webpush(browser.subscription, "x", vapid_config["privateKey"], vapid_config["subject"])
    fields = dict(
        item.strip().split("=", 1)
        for item in captured["headers"]["Authorization"].partition(" ")[2].split(",")
    )
    assert _b64url(base64.urlsafe_b64decode(fields["k"] + "==")) == vapid_config["publicKey"]


def test_custom_vapid_settings_override_runtime_defaults_and_reject_partial_pair(browser, vapid, monkeypatch):
    """
    显式成对配置的 VAPID 密钥与主体应优先于运行期默认值；仅配置单边密钥时不得混用默认值。
    """
    from app.runtime.config import settings

    other = Vapid()
    other.generate_keys()
    other_private = _b64url(other.private_key.private_numbers().private_value.to_bytes(32, "big"))
    other_public = _b64url(
        other.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    default_public = _b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )

    monkeypatch.setattr(
        settings,
        "_runtime_defaults",
        {
            "VAPID_PUBLIC_KEY": default_public,
            "VAPID_PRIVATE_KEY": vapid.private_key,
        },
    )
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", other_public)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", other_private)
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:custom@example.com")

    vapid_config = settings.VAPID
    assert vapid_config == {
        "subject": "mailto:custom@example.com",
        "publicKey": other_public,
        "privateKey": other_private,
    }
    captured, patcher = _post()
    with patcher:
        send_webpush(browser.subscription, "x", vapid_config["privateKey"], vapid_config["subject"])
    fields = dict(
        item.strip().split("=", 1)
        for item in captured["headers"]["Authorization"].partition(" ")[2].split(",")
    )
    assert _b64url(base64.urlsafe_b64decode(fields["k"] + "==")) == other_public

    # 仅配置公钥或仅配置私钥时必须失效，避免公私钥错配。
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", None)
    partial_public = settings.VAPID
    assert partial_public["publicKey"] == ""
    assert partial_public["privateKey"] == ""

    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", other_private)
    partial_private = settings.VAPID
    assert partial_private["publicKey"] == ""
    assert partial_private["privateKey"] == ""


@pytest.mark.parametrize("endpoint", [
    "http://push.example.com/secret", "https://user:secret@push.example.com/token",
    "https://127.0.0.1/private-token", "https://169.254.169.254/metadata-token",
    "https://[::1]/private-token", "https://push.example.com/token#fragment",
])
def test_invalid_or_private_push_urls_are_rejected_before_http(browser, vapid, endpoint):
    """
    非 HTTPS、URL 凭据、片段和私网字面量在创建 HTTP 客户端前拒绝。

    :param browser: 离线浏览器密钥
    :param vapid: 离线服务端签名密钥
    :param endpoint: 待拒绝的完整推送端点
    """
    captured, patcher = _post()
    with patcher, pytest.raises(WebPushDeliveryError) as error:
        send_webpush({**browser.subscription, "endpoint": endpoint}, "x", vapid.private_key, "mailto:a@b.c")
    assert captured == {}
    assert endpoint not in str(error.value)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "10.0.0.1"], []])
def test_push_dns_policy_rejects_private_mixed_or_missing_results(browser, vapid, public_push_dns, addresses):
    """
    宿主 DNS 策略拒绝私网、混合地址和空结果，不建立 HTTP 请求。

    :param browser: 离线浏览器密钥
    :param vapid: 离线服务端签名密钥
    :param public_push_dns: 可配置的离线 DNS 解析端口
    :param addresses: DNS 本次返回的地址集合
    """
    public_push_dns.resolve = lambda _hostname: addresses
    captured, patcher = _post()
    with patcher, pytest.raises(WebPushDeliveryError, match="公网安全检查"):
        send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c")
    assert captured == {}


def test_push_redirect_is_rejected_and_response_body_never_leaks(browser, vapid):
    """
    推送不追踪任何重定向，远端响应正文不会进入可被日志记录的错误消息。

    :param browser: 离线浏览器密钥
    :param vapid: 离线服务端签名密钥
    """
    captured, patcher = _post(302)
    with patcher, pytest.raises(WebPushDeliveryError) as error:
        send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c")
    assert captured["allow_redirects"] is False
    assert str(error.value) == "推送服务拒收：HTTP 302"
    assert "detail" not in str(error.value)


def test_transport_exception_does_not_expose_subscription_url_or_keys(browser, vapid):
    """
    底层网络异常可能包含完整能力 URL，协议适配器只暴露异常类别。

    :param browser: 离线浏览器密钥
    :param vapid: 离线服务端签名密钥
    """
    with patch("app.adapters.network.webpush.RequestUtils") as utils:
        utils.return_value.post_res.side_effect = RuntimeError(str(browser.subscription))
        with pytest.raises(WebPushDeliveryError) as error:
            send_webpush(browser.subscription, "x", vapid.private_key, "mailto:a@b.c")
    assert str(error.value) == "推送请求失败：RuntimeError"
