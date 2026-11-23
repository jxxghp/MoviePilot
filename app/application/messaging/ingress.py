"""
消息渠道回环进入宿主消息 API 的统一适配边界。
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from cachetools import TTLCache

from app.domain.admission import AdmissionProof
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.message import IncomingMessage
from app.schemas.types import NotificationChannel

BackgroundSubmitter = Callable[..., object]
VerifiedEventClaim = Callable[[str, float, float], bool]


_verified_receiver: Callable[[IncomingMessage], None] | None = None
_verified_event_claim: VerifiedEventClaim | None = None
_VERIFIED_EVENT_TTL = 120
_verified_events: TTLCache[tuple[str, str], bool] = TTLCache(maxsize=4096, ttl=120)
_verified_events_lock = threading.Lock()


def claim_verified_event(proof: AdmissionProof, event_id: int | str | None) -> bool:
    """
    独占认领处于有效时间窗的已验证事件，宿主由共享数据库仲裁。
    """
    now = time.time()
    if not event_id or proof.expires_at <= now:
        return False
    key = (proof.connection_id, str(event_id))
    claim = _verified_event_claim
    if claim is not None:
        fingerprint = sha256(dumps(key, ensure_ascii=True).encode()).hexdigest()
        try:
            return claim(fingerprint, now, now + _VERIFIED_EVENT_TTL)
        except Exception as error:
            logger.warning("无法认领已验证渠道事件，已停止处理：%s", type(error).__name__)
            return False
    with _verified_events_lock:
        if key in _verified_events or len(_verified_events) >= _verified_events.maxsize:
            return False
        _verified_events[key] = True
        return True


def configure_verified_event_claim(claim: VerifiedEventClaim | None) -> None:
    """
    登记宿主共享的事件认领端口，退出宿主时清除进程内验收缓存。
    """
    global _verified_event_claim
    with _verified_events_lock:
        _verified_event_claim = claim
        _verified_events.clear()


def configure_verified_message_receiver(receiver: Callable[[IncomingMessage], None] | None) -> None:
    """
    由组合根登记进程内的已验证平台消息消费入口。
    """
    global _verified_receiver
    _verified_receiver = receiver


def submit_verified_message(info: IncomingMessage, submit: BackgroundSubmitter) -> bool:
    """
    SDK 将已核验的消息直接交给受管执行器，来源证明无需经过公开 HTTP 入口。
    """
    receiver = _verified_receiver
    if receiver is None:
        return False
    try:
        return submit(receiver, info) is not False
    except RuntimeError:
        return False


PlatformPayloadReceiver = Callable[[bytes, str, NotificationChannel, str], None]
_platform_payload_receiver: PlatformPayloadReceiver | None = None


def configure_platform_payload_receiver(receiver: PlatformPayloadReceiver | None) -> None:
    """
    装配仅供宿主内 SDK 使用的解析入口。
    """
    global _platform_payload_receiver
    _platform_payload_receiver = receiver


def submit_platform_payload(
    payload: Mapping[str, Any], source: str | None, channel: NotificationChannel,
    connection_id: str | None, *, submit: BackgroundSubmitter,
) -> bool | None:
    """
    将已认证的 SDK 载荷直接交给宿主，未装配时允许兼容旧转发。
    """
    receiver = _platform_payload_receiver
    if receiver is None or not source or not connection_id:
        return None
    try:
        body = dumps(dict(payload), sort_keys=True).encode()
        return submit(receiver, body, source, channel, connection_id) is not False
    except (RuntimeError, TypeError, ValueError):
        logger.warning("提交已认证的渠道消息失败：source=%s", source)
        return False


@dataclass(frozen=True, slots=True)
class _MessageIngressRequest:
    """
    冻结同步与异步本地消息回环共用的请求参数。

    :param url: 宿主本地消息接收端点的完整 URL
    :param payload: 待回环投递的渠道消息数据
    :param headers: 包含宿主回环凭据的请求头
    :param source: 用于日志定位的渠道来源，无来源时使用占位值
    :param timeout: 回环 HTTP 请求超时时间，单位为秒
    """

    url: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    source: str
    timeout: float


class MessageIngressPort(Protocol):
    """
    宿主本地消息入口所需的最小同步、异步传输端口。
    """

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """
        同步投递 payload，并返回 HTTP 状态码或 None。

        :param url: 宿主本地消息接收端点 URL
        :param payload: 按 JSON 投递的渠道消息映射
        :param headers: 包含回环认证凭据的请求头
        :param timeout: 请求超时时间，单位为秒
        :return: 收到的 HTTP 状态码；未获得响应时为 None
        """
        ...

    async def async_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """
        异步投递 payload，并返回 HTTP 状态码或 None。

        :param url: 宿主本地消息接收端点 URL
        :param payload: 按 JSON 投递的渠道消息映射
        :param headers: 包含回环认证凭据的请求头
        :param timeout: 请求超时时间，单位为秒
        :return: 收到的 HTTP 状态码；未获得响应时为 None
        """
        ...


_message_ingress_lock = threading.Lock()
_message_ingress_port: Optional[MessageIngressPort] = None


def configure_message_ingress_port(
    port: MessageIngressPort,
) -> Optional[MessageIngressPort]:
    """
    由启动组合根装配消息回环传输，并返回旧实现供隔离环境恢复。
    """
    global _message_ingress_port
    with _message_ingress_lock:
        previous = _message_ingress_port
        _message_ingress_port = port
    return previous


def reset_message_ingress_port(port: Optional[MessageIngressPort] = None) -> None:
    """
    恢复指定消息回环传输；省略参数时回到未装配状态。
    """
    global _message_ingress_port
    with _message_ingress_lock:
        _message_ingress_port = port


def _message_ingress_snapshot() -> MessageIngressPort:
    """
    返回当前消息回环传输，未装配时稳定失败。
    """
    with _message_ingress_lock:
        port = _message_ingress_port
    if port is None:
        raise RuntimeError("消息回环端口尚未由启动组合根装配")
    return port


def build_message_ingress_url(source: str | None) -> str:
    """
    构造仅含非敏感来源参数的本地消息入口 URL。
    """
    query: dict[str, str] = {}
    if source:
        query["source"] = source
    base_url = f"http://127.0.0.1:{get_runtime_setting('PORT')}/api/v1/message"
    return f"{base_url}?{urlencode(query)}" if query else base_url


def build_message_ingress_headers() -> dict[str, str]:
    """
    把本地回环凭据放入请求头，避免访问日志记录明文 Token。
    """
    return {"X-API-KEY": str(get_runtime_setting("API_TOKEN") or "")}


def _message_ingress_request(
    payload: Mapping[str, Any],
    source: str | None,
    timeout: float,
) -> _MessageIngressRequest:
    """
    统一构造 URL、payload 副本和日志使用的来源标识。
    """
    return _MessageIngressRequest(
        url=build_message_ingress_url(source),
        payload=dict(payload),
        headers=build_message_ingress_headers(),
        source=source or "-",
        timeout=timeout,
    )


def _message_ingress_confirmed(
    request: _MessageIngressRequest,
    status_code: Optional[int],
) -> bool:
    """
    统一判断本地入口是否确认接收，并输出稳定失败原因。
    """
    if status_code is None:
        logger.error(f"转发渠道消息到本地入口失败：source={request.source} - 无响应")
        return False
    if status_code >= 400:
        logger.error(
            "转发渠道消息到本地入口失败："
            f"source={request.source} - HTTP {status_code}"
        )
        return False
    return True


def _message_ingress_failed(
    source: str | None,
    error: Exception,
) -> bool:
    """
    统一记录传输或组合根异常，并按关闭策略返回失败。
    """
    logger.error(
        f"转发渠道消息到本地入口失败：source={source or '-'} - {error}"
    )
    return False


def forward_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    timeout: float = 15,
) -> bool:
    """
    同步转发渠道 payload，统一判断本地入口是否确认接收。
    """
    try:
        request = _message_ingress_request(payload, source, timeout)
        status_code = _message_ingress_snapshot().post(
            request.url,
            request.payload,
            headers=request.headers,
            timeout=request.timeout,
        )
        return _message_ingress_confirmed(request, status_code)
    except Exception as error:
        return _message_ingress_failed(source, error)


async def async_forward_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    timeout: float = 15,
) -> bool:
    """
    异步转发渠道 payload，供自有事件循环的消息 SDK 复用同一确认语义。
    """
    try:
        request = _message_ingress_request(payload, source, timeout)
        status_code = await _message_ingress_snapshot().async_post(
            request.url,
            request.payload,
            headers=request.headers,
            timeout=request.timeout,
        )
        return _message_ingress_confirmed(request, status_code)
    except Exception as error:
        return _message_ingress_failed(source, error)


def submit_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    submit: BackgroundSubmitter,
    timeout: float = 15,
) -> bool:
    """
    把同步回环转发提交给调用方注入的受管后台执行器。
    """
    try:
        submit(
            forward_message_to_host,
            dict(payload),
            source,
            timeout=timeout,
        )
    except Exception as error:
        logger.error(f"提交渠道消息转发任务失败：source={source or '-'} - {error}")
        return False
    return True
