import json
from typing import Union, Tuple

from pywebpush import webpush, WebPushException

from app.core.config import global_vars, settings
from app.log import logger
from app.modules import _ModuleBase, checkMessage
from app.schemas import MessageChannel, Notification


class WebPushModule(_ModuleBase):
    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
        return "WebPush"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "webpush"

    @checkMessage(MessageChannel.WebPush)
    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
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
                    )
                except WebPushException as err:
                    logger.error(f"WebPush发送失败: {str(err)}")

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
