from typing import Optional, Union, List, Tuple, Any, Dict

from app.core.context import MediaInfo, Context
from app.helper.notification import NotificationHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.synologychat.synologychat import SynologyChat
from app.schemas import MessageChannel, CommingMessage, Notification, NotificationConf


class SynologyChatModule(_ModuleBase):
    _channel = MessageChannel.Telegram
    _configs: Dict[str, NotificationConf] = {}
    _clients: Dict[str, SynologyChat] = {}

    def init_module(self) -> None:
        """
        初始化模块
        """
        clients = NotificationHelper().get_clients()
        if not clients:
            return
        self._configs = {}
        self._clients = {}
        for client in clients:
            if client.type == "telegram" and client.enabled:
                self._configs[client.name] = client
                self._clients[client.name] = SynologyChat(**client.config)

    @staticmethod
    def get_name() -> str:
        return "Synology Chat"

    def get_client(self, name: str) -> Optional[SynologyChat]:
        """
        获取Telegram客户端
        """
        return self._clients.get(name)

    def get_config(self, name: str) -> Optional[NotificationConf]:
        """
        获取Telegram配置
        """
        return self._configs.get(name)

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        for name, client in self._clients.items():
            state = client.get_state()
            if not state:
                return False, f"Synology Chat {name} 未就续"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def checkMessage(self, message: Notification, source: str) -> bool:
        """
        检查消息渠道及消息类型，如不符合则不处理
        """
        # 检查消息渠道
        if message.channel and message.channel != self._channel:
            return False
        # 检查消息来源
        if message.source and message.source != source:
            return False
        # 检查消息类型开关
        if message.mtype:
            conf = self.get_config(source)
            if conf:
                switchs = conf.switchs or []
                if message.mtype.value not in switchs:
                    return False
        return True

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
            # 来源
            source = args.get("source")
            if not source:
                return None
            client = self.get_client(source)
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
                logger.info(f"收到SynologyChat消息：userid={user_id}, username={user_name}, text={text}")
                return CommingMessage(channel=MessageChannel.SynologyChat,
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
        for conf in self._configs.values():
            if not self.checkMessage(message, conf.name):
                continue
            client = self.get_client(conf.name)
            if client:
                client.send_msg(title=message.title, text=message.text,
                                image=message.image, userid=message.userid, link=message.link)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息体
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self._configs.values():
            if not self.checkMessage(message, conf.name):
                continue
            client = self.get_client(conf.name)
            if client:
                client.send_meidas_msg(title=message.title, medias=medias,
                                       userid=message.userid)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message: 消息体
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self._configs.values():
            if not self.checkMessage(message, conf.name):
                continue
            client = self.get_client(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=message.userid, link=message.link)
