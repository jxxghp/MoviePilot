"""
宿主认证入站及显式旧回环适配；两条路径各自提交，不能隐式回退。
"""

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from json import dumps
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from cachetools import TTLCache

from app.domain.admission import AdmissionProof
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel

BackgroundSubmitter = Callable[..., object]
VerifiedEventClaim = Callable[[str, float, float], bool]


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


PlatformPayloadReceiver = Callable[[bytes, str, NotificationChannel, str, str, str, str, int], None]
_platform_payload_receiver: PlatformPayloadReceiver | None = None


def configure_platform_payload_receiver(receiver: PlatformPayloadReceiver | None) -> None:
    """
    装配仅供宿主内 SDK 使用的解析入口。
    """
    global _platform_payload_receiver
    _platform_payload_receiver = receiver


def bind_platform_payload(config: NotificationConf) -> Callable[..., bool] | None:
    """
    由宿主模块冻结当前连接的解析 owner、身份空间和策略版本，不赋予成员权限。

    :param config: 宿主已经投影的连接配置；客户端不能从平台报文重建此绑定
    :return: 只接收载荷和受管执行器的绑定提交函数；连接未完整装配时为空
    """
    from app.application.messaging.channel.platform import platform_catalog

    platform = platform_catalog().get(config.type or "")
    if (config.enabled is not True or config.interaction_enabled is False
            or (config.interaction_policy is not None and not config.interaction_policy.enabled)
            or platform is None or not platform.interaction or not config.id or not config.name):
        return None
    return partial(
        submit_platform_payload, source=config.name, channel=NotificationChannel(platform.channel),
        connection_id=config.id, provider=platform.provider,
        identity_namespace=config.identity_namespace or config.id, parser_owner=platform.module_id,
        policy_revision=config.interaction_policy.revision if config.interaction_policy else 0,
    )


def submit_bound_payload(
    payload: Mapping[str, Any], bound: Callable[..., bool] | None, source: str | None,
    *, submit: BackgroundSubmitter,
) -> bool:
    """
    SDK 只调用模块装配的绑定入口；缺少绑定时明确拒绝，保留布尔确认兼容。

    :param payload: 已经通过平台协议认证的载荷
    :param bound: 模块创建客户端时提供的绑定函数，普通公开 SDK 不导出其构造能力
    :param source: 只用于诊断的实例名称
    :param submit: 客户端生命周期拥有的受管执行器
    :return: 任务已接受时为 True；不表示审批或业务操作成功
    """
    if bound is None:
        return _reject_platform_payload("identity_missing", source)
    try:
        return bound(payload, submit=submit)
    except Exception as error:
        return _reject_platform_payload(f"submit_failed:{type(error).__name__}", source)


def _reject_platform_payload(reason: str, source: str | None) -> bool:
    """
    输出不包含凭据或原始报文的稳定拒绝原因。
    """
    logger.warning(f"渠道入站未接受：reason={reason}, source={source or '-'}")
    return False


def _deliver_platform_payload(receiver: PlatformPayloadReceiver, *args: Any) -> None:
    """
    执行排队事件前再次复核生命周期，停机或重装后不使用旧接收器。
    """
    if runtime_stop_state.is_system_stopped:
        _reject_platform_payload("stopping", str(args[1]))
        return
    if receiver is not _platform_payload_receiver:
        _reject_platform_payload("receiver_changed", str(args[1]))
        return
    receiver(*args)


def _observe_platform_submission(source: str | None, future: Future[Any]) -> None:
    """
    记录已接受任务的取消或失败；不重发、不切换路径，也不混淆业务授权与队列确认。
    """
    if future.cancelled():
        _reject_platform_payload("submission_cancelled", source)
    elif (error := future.exception()) is not None:
        _reject_platform_payload(f"processing_failed:{type(error).__name__}", source)


def submit_platform_payload(
    payload: Mapping[str, Any], source: str | None, channel: NotificationChannel,
    connection_id: str | None, *, submit: BackgroundSubmitter, provider: str | None = None,
    identity_namespace: str | None = None, parser_owner: str | None = None,
    policy_revision: int | None = None,
) -> bool:
    """
    提交宿主内已认证的 SDK 事件，所有拒绝都在当前路径结束。

    :param payload: 平台验证的载荷，报文中的身份元数据不能覆盖绑定
    :param source: 创建 SDK 实例时绑定的配置名称
    :param channel: 注册表声明的传输类型
    :param connection_id: 创建 SDK 实例时绑定的稳定连接 ID
    :param submit: 受管后台执行器；拒绝或关闭时不得转为 HTTP 回环
    :param provider: 注册表声明的协议 owner
    :param identity_namespace: 连接不可变身份空间
    :param parser_owner: 该 provider 在既有模块调度器中的明确宿主 ID
    :param policy_revision: 创建 SDK 实例时的交互策略版本
    :return: 队列已接受任务时为 True；不表示业务操作获得授权
    """
    from app.application.security.authorization import get_channel_authorization

    if runtime_stop_state.is_system_stopped:
        return _reject_platform_payload("stopping", source)
    if get_channel_authorization() is None:
        return _reject_platform_payload("not_ready", source)
    receiver = _platform_payload_receiver
    if receiver is None:
        return _reject_platform_payload("not_ready", source)
    if not all((source, connection_id, provider, identity_namespace, parser_owner)) or policy_revision is None:
        return _reject_platform_payload("identity_missing", source)
    if channel in {NotificationChannel.Web, NotificationChannel.WebAgent}:
        return _reject_platform_payload("reserved_identity", source)
    try:
        body = dumps(dict(payload), sort_keys=True).encode()
        accepted = submit(
            _deliver_platform_payload, receiver, body, source, channel, connection_id,
            provider, identity_namespace, parser_owner, policy_revision,
        )
        if accepted is False or (isinstance(accepted, Future) and accepted.cancelled()):
            return _reject_platform_payload("submit_rejected", source)
        if isinstance(accepted, Future):
            accepted.add_done_callback(partial(_observe_platform_submission, source))
        return True
    except Exception as error:
        return _reject_platform_payload(f"submit_failed:{type(error).__name__}", source)


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
    if runtime_stop_state.is_system_stopped:
        return False
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
    if runtime_stop_state.is_system_stopped:
        return False
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
    if runtime_stop_state.is_system_stopped:
        return False
    try:
        accepted = submit(
            forward_message_to_host,
            dict(payload),
            source,
            timeout=timeout,
        )
    except Exception as error:
        logger.error(f"提交渠道消息转发任务失败：source={source or '-'} - {error}")
        return False
    return accepted is not False
