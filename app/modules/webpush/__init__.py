import json
from typing import Union, Tuple

from pywebpush import webpush, WebPushException

from app.runtime.config import global_vars, settings
from app.runtime.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.schemas import Notification
from app.schemas.types import ModuleType, MessageChannel


class WebPushModule(_ModuleBase, _MessageBase):
    """通过浏览器 Web Push 通道发送通知消息。"""

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=self.get_name().lower())
        self._channel = MessageChannel.WebPush

    @staticmethod
    def get_name() -> str:
        return "WebPush"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> MessageChannel:
        """
        获取模块子类型
        """
        return MessageChannel.WebPush

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 6

    def stop(self):
        """Web Push 模块没有需要释放的长连接资源。"""
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """Web Push 使用全局 VAPID 配置，不提供模块级设置。"""
        pass

    def post_message(self, message: Notification, **kwargs) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            webpush_users = conf.config.get("WEBPUSH_USERNAME") or ""
            if webpush_users:
                # 设定了接收用户时，非该用户的消息不接收
                if not message.username or message.username not in webpush_users.split(","):
                    continue
            if not message.title and not message.text:
                logger.warn("标题和内容不能同时为空")
                return
            try:
                if message.title:
                    caption = message.title
                    content = message.text
                else:
                    caption = message.text
                    content = ""
                for sub in global_vars.get_subscriptions():
                    logger.debug(f"给 {sub} 发送WebPush：{caption} {content}")
                    try:
                        endpoint = sub.get("endpoint")
                        webpush_options = {}
                        if endpoint and "notify.windows.com" in endpoint:
                            webpush_options = {
                                "ttl": 86400,
                                "headers": {"X-WNS-Cache-Policy": "cache"},
                            }
                        webpush(
                            subscription_info=sub,
                            data=json.dumps({
                                "title": caption,
                                "body": content,
                                "url": message.link or "/?shotcut=message"
                            }),
                            vapid_private_key=settings.VAPID.get("privateKey"),
                            vapid_claims={
                                "sub": settings.VAPID.get("subject")
                            },
                            **webpush_options,
                        )
                    except WebPushException as err:
                        logger.error(f"WebPush发送失败: {str(err)}")
                        response = getattr(err, "response", None)
                        status_code = getattr(response, "status_code", None) or getattr(
                            response,
                            "status",
                            None,
                        )
                        if status_code in {404, 410} and global_vars.remove_subscription(sub):
                            logger.info(f"已移除失效WebPush订阅: {sub.get('endpoint')}")

            except Exception as msg_e:
                logger.error(f"发送消息失败：{msg_e}")
