"""
按通知配置向已登记浏览器订阅投递 Web Push 消息。
"""

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple, Union

from app.adapters.network.webpush import WebPushDeliveryError, send_webpush
from app.application.messaging.delivery import NotificationParts
from app.application.webpush import WebPushRegistration, WebPushService, get_webpush_service
from app.modules import _MessageBase, _ModuleBase
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.delivery import DeliveryPartOutcome, DeliveryStatus
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, NotificationChannel


def _push_error_status(status_code: Any) -> DeliveryStatus:
    """
    HTTP 拒绝与没有可验证响应的发送分开记录。
    """
    if not isinstance(status_code, int) or status_code == 408 or status_code >= 500:
        return "unknown"
    if status_code == 429:
        return "retryable_failure"
    if status_code in {404, 410}:
        return "skipped"
    return "permanent_failure"


def _push_retry_after(value: str | None) -> int | None:
    """
    读取秒数或 HTTP 日期；无有效提示时由重试调度采用自身退避。
    """
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            seconds = int((deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds)


class WebPushModule(_ModuleBase, _MessageBase[object]):
    """
    通过浏览器 Web Push 通道发送通知消息。

    :cvar _notification_user_target_keys: 私人通知只接受服务器生成的浏览器推送账号身份
    """

    _notification_user_target_keys = ("webpush",)

    def init_module(self) -> None:
        """
        读取 Web Push 通知配置并声明浏览器推送渠道。
        """
        super().init_service(service_name=self.get_name().lower())
        self._channel = NotificationChannel.WebPush

    @staticmethod
    def get_name() -> str:
        """
        返回浏览器推送模块的展示名称。
        """
        return "WebPush"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        获取模块子类型。
        """
        return NotificationChannel.WebPush

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 6

    def stop(self) -> None:
        """
        Web Push 模块没有需要释放的长连接资源。
        """
        pass

    def test(self) -> Tuple[bool, str]:
        """
        返回模块可用状态，不向浏览器推送端点发起探测。

        :return: 固定成功结果；实际投递错误由发送时记录
        """
        return True, ""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        Web Push 使用全局 VAPID 配置，不提供模块级设置。

        :return: 始终为 None，表示不检查模块级配置项
        """
        pass

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        向匹配通知配置的浏览器订阅逐个投递消息。

        投递失败仅记录日志，不中断其他订阅；端点返回 404 或 410 时移除失效订阅。

        :param message: 待发送的标题、正文、目标用户与跳转链接
        :param kwargs: 为模块分发接口保留的扩展参数，本实现不使用
        """
        message = self.prepare_message(message)
        for conf, message in self.iter_message_configs(message):
            webpush_users = (conf.config or {}).get("WEBPUSH_USERNAME") or ""
            if webpush_users:
                # 设定了接收用户时，非该用户的消息不接收
                if not message.username or message.username not in webpush_users.split(","):
                    continue
            if not message.title and not message.text:
                logger.warn("标题和内容不能同时为空")
                return
            data = json.dumps({
                "title": message.title or message.text,
                "body": message.text if message.title else "",
                "url": message.link or "/?shotcut=message",
            })
            owners = self._notification_owners(message)
            if not owners:
                self.record_delivery(message, conf, None, status="skipped", code="no_target")
                continue
            try:
                service = get_webpush_service()
            except RuntimeError:
                self.record_delivery(message, conf, None, status="retryable_failure", code="client_unavailable")
                continue
            registrations = service.for_owners(owners)
            if not registrations:
                self.record_delivery(message, conf, None, status="skipped", code="no_device")
                continue
            self._post_tracked(message, conf, service, registrations, data)

    @staticmethod
    def _post_tracked(
        message: Message, conf: NotificationConf, service: WebPushService,
        registrations: tuple[WebPushRegistration, ...], data: str,
    ) -> None:
        """
        冻结设备版本，恢复时只提交仍然有效且已登记的分段。
        """
        parts = NotificationParts(NotificationChannel.WebPush, conf.id, conf.identity_namespace, part_offset=message.attachment_part_offset)
        devices = tuple(
            (sha256(json.dumps((item.endpoint, item.owner, item.revision)).encode()).hexdigest(), item)
            for item in registrations
        )
        parts.plan(tuple((target, 0, data) for target, _ in devices))
        for target, registration in devices:
            result = parts.send_result(target, 0, data, True, partial(
                WebPushModule._send_registration, service, registration, data,
            ))
            message.record_delivery_result(result)

    @staticmethod
    def _send_registration(
        service: WebPushService, registration: WebPushRegistration, data: str,
    ) -> DeliveryPartOutcome:
        """
        提交单个订阅，记录推送服务的确认结果与限流等待时间。
        """
        if not service.is_current(registration):
            return "skipped", "subscription_changed"
        options: Dict[str, Any] = {}
        if "notify.windows.com" in registration.endpoint:
            options = {"ttl": 86400, "headers": {"X-WNS-Cache-Policy": "cache"}}
        vapid = get_runtime_setting("VAPID") or {}
        try:
            send_webpush(
                subscription=registration.subscription(), data=data,
                vapid_private_key=str(vapid.get("privateKey") or ""), vapid_subject=str(vapid.get("subject") or ""), **options,
            )
        except WebPushDeliveryError as err:
            logger.error(f"WebPush发送失败: {err}")
            response = err.response
            status_code = getattr(response, "status_code", None) or getattr(response, "status", None)
            retry_after = _push_retry_after((getattr(response, "headers", None) or {}).get("Retry-After"))
            if status_code in {404, 410} and service.remove_expired(registration):
                logger.info("已移除当前版本的失效WebPush订阅")
            code = f"http_{status_code}" if isinstance(status_code, int) else "push_unconfirmed"
            return _push_error_status(status_code), code, None, retry_after
        except Exception as error:
            logger.error(f"WebPush发送结果未确认: {type(error).__name__}")
            return "unknown", "push_unconfirmed"
        return "confirmed", None

    def _notification_owners(self, message: Message) -> tuple[str, ...]:
        """
        读取通知链注入的不可变账号集合，缺少目标时不隐式广播。

        :param message: 带明确通知范围及服务器账号目标的消息
        :return: 单账号身份，或显式全体路由的账号身份集合
        """
        target = (self.get_notification_targets(message) or {}).get("webpush")
        if isinstance(target, str):
            return (target,) if target else ()
        if message.notification_route_scope == "all" and isinstance(target, tuple):
            return tuple(dict.fromkeys(owner for owner in target if isinstance(owner, str) and owner))
        return ()
