"""消息渠道回环进入宿主消息 API 的统一适配边界。"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

BackgroundSubmitter = Callable[..., object]


@dataclass(frozen=True, slots=True)
class _MessageIngressRequest:
    """冻结同步与异步本地消息回环共用的请求参数。"""

    url: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    source: str
    timeout: float


class MessageIngressPort(Protocol):
    """宿主本地消息入口所需的最小同步、异步传输端口。"""

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """同步投递 payload，并返回 HTTP 状态码或 None。"""
        ...

    async def async_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """异步投递 payload，并返回 HTTP 状态码或 None。"""
        ...


_message_ingress_lock = threading.Lock()
_message_ingress_port: Optional[MessageIngressPort] = None


def configure_message_ingress_port(
    port: MessageIngressPort,
) -> Optional[MessageIngressPort]:
    """由启动组合根装配消息回环传输，并返回旧实现供隔离环境恢复。"""
    global _message_ingress_port
    with _message_ingress_lock:
        previous = _message_ingress_port
        _message_ingress_port = port
    return previous


def reset_message_ingress_port(port: Optional[MessageIngressPort] = None) -> None:
    """恢复指定消息回环传输；省略参数时回到未装配状态。"""
    global _message_ingress_port
    with _message_ingress_lock:
        _message_ingress_port = port


def _message_ingress_snapshot() -> MessageIngressPort:
    """返回当前消息回环传输，未装配时稳定失败。"""
    with _message_ingress_lock:
        port = _message_ingress_port
    if port is None:
        raise RuntimeError("消息回环端口尚未由启动组合根装配")
    return port


def build_message_ingress_url(source: str | None) -> str:
    """构造仅含非敏感来源参数的本地消息入口 URL。"""
    query: dict[str, str] = {}
    if source:
        query["source"] = source
    base_url = f"http://127.0.0.1:{get_runtime_setting('PORT')}/api/v1/message"
    return f"{base_url}?{urlencode(query)}" if query else base_url


def build_message_ingress_headers() -> dict[str, str]:
    """把本地回环凭据放入请求头，避免访问日志记录明文 Token。"""
    return {"X-API-KEY": str(get_runtime_setting("API_TOKEN") or "")}


def _message_ingress_request(
    payload: Mapping[str, Any],
    source: str | None,
    timeout: float,
) -> _MessageIngressRequest:
    """统一构造 URL、payload 副本和日志使用的来源标识。"""
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
    """统一判断本地入口是否确认接收，并输出稳定失败原因。"""
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
    """统一记录传输或组合根异常，并按关闭策略返回失败。"""
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
    """同步转发渠道 payload，统一判断本地入口是否确认接收。"""
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
    """异步转发渠道 payload，供自有事件循环的消息 SDK 复用同一确认语义。"""
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
    """把同步回环转发提交给调用方注入的受管后台执行器。"""
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
