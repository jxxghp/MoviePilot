"""
Web Push 协议发送（RFC 8030 投递、RFC 8291 aes128gcm 加密、RFC 8292 VAPID 签名）。

直接使用 http_ece 与 py_vapid 完成加密和签名，经 RequestUtils 投递，不导入 pywebpush：
后者只是这两个库的薄封装，却会在导入时加载 aiohttp。请求头、VAPID 声明与 pywebpush
2.3 的 ``webpush()`` 保持一致。
"""

import base64
import time
from typing import Any, Dict, Mapping, Optional, cast
from urllib.parse import urlparse

import http_ece
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from app.adapters.network.http import RequestUtils

# VAPID JWT 有效期；与 pywebpush 默认一致，推送服务要求不超过 24 小时。
_VAPID_EXPIRE_SECONDS = 12 * 60 * 60
# 推送服务返回 2xx 中不超过 202 的状态码视为已接收。
_MAX_ACCEPTED_STATUS = 202
_DEFAULT_TIMEOUT_SECONDS = 20


class WebPushDeliveryError(Exception):
    """
    Web Push 投递失败。

    ``response`` 为推送服务的 HTTP 响应，网络失败或订阅数据无效时为 None；
    调用方据其状态码（404/410）判断订阅是否已失效。
    """

    def __init__(self, message: str, response: Any = None):
        super().__init__(message)
        self.response = response


def _b64url_decode(value: str) -> bytes:
    """解码浏览器订阅中去掉填充的 base64url 字段。"""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _vapid_headers(endpoint: str, private_key: str, subject: str) -> Dict[str, str]:
    """按推送端点源站生成 VAPID Authorization 请求头。"""
    url = urlparse(endpoint)
    claims = {
        "sub": subject,
        "aud": f"{url.scheme}://{url.netloc}",
        "exp": int(time.time()) + _VAPID_EXPIRE_SECONDS,
    }
    return cast(Dict[str, str], Vapid.from_string(private_key=private_key).sign(claims))


def _encrypt(subscription: Mapping[str, Any], payload: bytes) -> bytes:
    """用订阅公钥与 auth 密钥按 aes128gcm 加密消息体，每次使用新的临时 ECDH 密钥。"""
    keys = subscription.get("keys") or {}
    if not keys.get("p256dh") or not keys.get("auth"):
        raise WebPushDeliveryError("订阅缺少 p256dh 或 auth 密钥")
    receiver_key = _b64url_decode(keys["p256dh"])
    if len(receiver_key) != 65 or receiver_key[0] != 0x04:
        raise WebPushDeliveryError("订阅的 p256dh 公钥格式无效")
    return cast(bytes, http_ece.encrypt(
        payload,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=receiver_key,
        auth_secret=_b64url_decode(keys["auth"]),
        version="aes128gcm",
    ))


def send_webpush(
        subscription: Mapping[str, Any],
        data: str,
        vapid_private_key: str,
        vapid_subject: str,
        ttl: int = 0,
        headers: Optional[Mapping[str, str]] = None,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """
    向单个浏览器订阅投递一条加密通知。

    :param subscription: 浏览器 PushSubscription JSON（endpoint 与 keys.p256dh/auth）
    :param data: 通知正文，通常是 Service Worker 解析的 JSON 字符串
    :param vapid_private_key: VAPID 私钥（base64url 编码的原始私钥或 DER）
    :param vapid_subject: VAPID 联系方式，形如 ``mailto:...`` 或站点 URL
    :param ttl: 接收端离线时推送服务保留消息的秒数，0 表示不保留
    :param headers: 端点专用的附加请求头，例如 WNS 缓存策略
    :param timeout: 请求超时秒数
    :return: 推送服务的 HTTP 响应
    :raises WebPushDeliveryError: 订阅无效、网络失败或推送服务拒收
    """
    endpoint = subscription.get("endpoint")
    if not endpoint:
        raise WebPushDeliveryError("订阅缺少 endpoint")
    if not vapid_private_key:
        raise WebPushDeliveryError("未配置 VAPID 私钥")
    request_headers = {
        **(headers or {}),
        **_vapid_headers(endpoint, vapid_private_key, vapid_subject),
        "Content-Encoding": "aes128gcm",
        "TTL": str(ttl),
    }
    response = RequestUtils(
        headers=request_headers, timeout=timeout, verify=True
    ).post_res(endpoint, data=_encrypt(subscription, data.encode("utf-8")))
    if response is None:
        raise WebPushDeliveryError("推送服务无响应")
    if response.status_code > _MAX_ACCEPTED_STATUS:
        raise WebPushDeliveryError(
            f"推送服务拒收：{response.status_code} {response.reason}，{response.text[:200]}",
            response=response,
        )
    return response
