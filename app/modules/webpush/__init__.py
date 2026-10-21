"""
按通知配置向已登记浏览器订阅投递 Web Push 消息。
"""

import json
from typing import Any, Dict, Optional, Tuple, Union

from app.adapters.network.webpush import WebPushDeliveryError, send_webpush
from app.modules import _MessageBase, _ModuleBase
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.webpush import webpush_registry
from app.schemas.message import Message
from app.schemas.types import ModuleType, NotificationChannel


class WebPushModule(_ModuleBase, _MessageBase[object]):
    """
    通过浏览器 Web Push 通道发送通知消息。
    """

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
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            webpush_users = (conf.config or {}).get("WEBPUSH_USERNAME") or ""
            if webpush_users:
                # 设定了接收用户时，非该用户的消息不接收
                if not message.username or message.username not in webpush_users.split(","):
                    continue
            if not message.title and not message.text:
                logger.warn("标题和内容不能同时为空")
                return
            try:
                if message.title:
                    caption: Optional[str] = message.title
                    content = message.text
                else:
                    caption = message.text
                    content = ""
                for sub in webpush_registry.list():
                    logger.debug(f"给 {sub} 发送WebPush：{caption} {content}")
                    try:
                        endpoint = sub.get("endpoint")
                        webpush_options: Dict[str, Any] = {}
                        if endpoint and "notify.windows.com" in endpoint:
                            webpush_options = {
                                "ttl": 86400,
                                "headers": {"X-WNS-Cache-Policy": "cache"},
                            }
                        send_webpush(
                            subscription=sub,
                            data=json.dumps({
                                "title": caption,
                                "body": content,
                                "url": message.link or "/?shotcut=message"
                            }),
                            vapid_private_key=get_runtime_setting('VAPID').get("privateKey"),
                            vapid_subject=get_runtime_setting('VAPID').get("subject"),
                            **webpush_options,
                        )
                    except WebPushDeliveryError as err:
                        logger.error(f"WebPush发送失败: {str(err)}")
                        response = getattr(err, "response", None)
                        status_code = getattr(response, "status_code", None) or getattr(
                            response,
                            "status",
                            None,
                        )
                        if status_code in {404, 410} and webpush_registry.remove(sub):
                            logger.info(f"已移除失效WebPush订阅: {sub.get('endpoint')}")

            except Exception as msg_e:
                logger.error(f"发送消息失败：{msg_e}")
