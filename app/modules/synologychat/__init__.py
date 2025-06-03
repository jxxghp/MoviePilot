from typing import Optional, Union, List, Tuple, Any

from app.core.config import on_config_change
from app.core.context import MediaInfo, Context
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.synologychat.synologychat import SynologyChat
from app.schemas import MessageChannel, CommingMessage, Notification
from app.schemas.types import ModuleType, SystemConfigKey


class SynologyChatModule(_ModuleBase, _MessageBase[SynologyChat]):

    @on_config_change([SystemConfigKey.Notifications.value])
    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=SynologyChat.__name__.lower(),
                             service_type=SynologyChat)
        self._channel = MessageChannel.SynologyChat

    @staticmethod
    def get_name() -> str:
        return "Synology Chat"

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
        return MessageChannel.SynologyChat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 5

    def stop(self):
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state = client.get_state()
            if not state:
                return False, f"Synology Chat {name} 未就续"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 渠道、消息体
        """
        try:
            # 获取服务配置
            client_config = self.get_config(source)
            if not client_config:
                return None
            client: SynologyChat = self.get_instance(client_config.name)
            if not client:
                return None
            # 解析消息
            message: dict = form
            if not message:
                return None
            # 校验token
            token = message.get("token")
            if not token or not client.check_token(token):
                return None
            # 文本
            text = message.get("text")
            # 用户ID
            user_id = int(message.get("user_id"))
            # 获取用户名
            user_name = message.get("username")
            if text and user_id:
                logger.info(f"收到来自 {client_config.name} 的SynologyChat消息："
                            f"userid={user_id}, username={user_name}, text={text}")
                return CommingMessage(channel=MessageChannel.SynologyChat, source=client_config.name,
                                      userid=user_id, username=user_name, text=text)
        except Exception as err:
            logger.debug(f"解析SynologyChat消息失败：{str(err)}")
        return None

    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息体
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get('synologychat_userid')
                if not userid:
                    logger.warn(f"用户没有指定 SynologyChat用户ID，消息无法发送")
                    return
            client: SynologyChat = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, text=message.text,
                                image=message.image, userid=userid, link=message.link)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息体
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: SynologyChat = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=message.userid)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message: 消息体
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: SynologyChat = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=message.userid, link=message.link)
