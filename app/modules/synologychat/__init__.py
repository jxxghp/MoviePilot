from typing import Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.log import logger
from app.modules import _ModuleBase, checkMessage
from app.modules.synologychat.synologychat import SynologyChat
from app.schemas import MessageChannel, CommingMessage, Notification


class SynologyChatModule(_ModuleBase):
    synologychat: SynologyChat = None

    def init_module(self) -> None:
        self.synologychat = SynologyChat()

    @staticmethod
    def get_name() -> str:
        return "Synology Chat"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        state = self.synologychat.get_state()
        if state:
            return True, ""
        return False, "SynologyChat未就续，请检查参数设置、网络连接以及机器人是否可见"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "synologychat"

    def message_parser(self, body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 渠道、消息体
        """
        try:
            message: dict = form
            if not message:
                return None
            # 校验token
            token = message.get("token")
            if not token or not self.synologychat.check_token(token):
                return None
            # 文本
            text = message.get("text")
            # 用户ID
            user_id = int(message.get("user_id"))
            # 获取用户名
            user_name = message.get("username")
            if text and user_id:
                logger.info(f"收到SynologyChat消息：userid={user_id}, username={user_name}, text={text}")
                return CommingMessage(channel=MessageChannel.SynologyChat,
                                      userid=user_id, username=user_name, text=text)
        except Exception as err:
            logger.debug(f"解析SynologyChat消息失败：{str(err)}")
        return None

    @checkMessage(MessageChannel.SynologyChat)
    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息体
        :return: 成功或失败
        """
        self.synologychat.send_msg(title=message.title, text=message.text,
                                   image=message.image, userid=message.userid, link=message.link)

    @checkMessage(MessageChannel.SynologyChat)
    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param message: 消息体
        :param medias: 媒体列表
        :return: 成功或失败
        """
        return self.synologychat.send_meidas_msg(title=message.title, medias=medias,
                                                 userid=message.userid)

    @checkMessage(MessageChannel.SynologyChat)
    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param message: 消息体
        :param torrents: 种子列表
        :return: 成功或失败
        """
        return self.synologychat.send_torrents_msg(title=message.title, torrents=torrents,
                                                   userid=message.userid, link=message.link)
