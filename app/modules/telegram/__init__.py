import json
from typing import Optional, Union, List, Tuple, Any, Dict

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.helper.notification import NotificationHelper
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.telegram.telegram import Telegram
from app.schemas import MessageChannel, CommingMessage, Notification


class TelegramModule(_ModuleBase, _MessageBase):

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
                self._clients[client.name] = Telegram(**client.config, name=client.name)

    @staticmethod
    def get_name() -> str:
        return "Telegram"

    def stop(self):
        """
        停止模块
        """
        for client in self._clients.values():
            client.stop()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        for name, client in self._clients.items():
            state = client.get_state()
            if not state:
                return False, f"Telegram {name} 未就续"
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
        """
            {
                'update_id': ,
                'message': {
                    'message_id': ,
                    'from': {
                        'id': ,
                        'is_bot': False,
                        'first_name': '',
                        'username': '',
                        'language_code': 'zh-hans'
                    },
                    'chat': {
                        'id': ,
                        'first_name': '',
                        'username': '',
                        'type': 'private'
                    },
                    'date': ,
                    'text': ''
                }
            }
        """
        # 获取渠道
        client: Telegram = self.get_client(source)
        if not client:
            return None
        # 获取配置
        config = self.get_config(source)
        if not config:
            return None
        # 校验token
        token = args.get("token")
        if not token or token != settings.API_TOKEN:
            return None
        try:
            message: dict = json.loads(body)
        except Exception as err:
            logger.debug(f"解析Telegram消息失败：{str(err)}")
            return None
        if message:
            text = message.get("text")
            user_id = message.get("from", {}).get("id")
            # 获取用户名
            user_name = message.get("from", {}).get("username")
            if text:
                logger.info(f"收到来自 {source} 的Telegram消息：userid={user_id}, username={user_name}, text={text}")
                # 检查权限
                admin_users = config.config.get("admins")
                user_list = config.config.get("users")
                chat_id = config.config.get("chat_id")
                if text.startswith("/"):
                    if admin_users \
                            and str(user_id) not in admin_users.split(',') \
                            and str(user_id) != chat_id:
                        client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                        return None
                else:
                    if user_list \
                            and not str(user_id) in user_list.split(','):
                        logger.info(f"用户{user_id}不在用户白名单中，无法使用此机器人")
                        client.send_msg(title="你不在用户白名单中，无法使用此机器人", userid=user_id)
                        return None
                return CommingMessage(channel=MessageChannel.Telegram, source=source,
                                      userid=user_id, username=user_name, text=text)
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
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get('telegram_userid')
                if not userid:
                    logger.warn(f"用户没有指定 Telegram用户ID，消息无法发送")
                    return
            client: Telegram = self.get_client(conf.name)
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
        for conf in self._configs.values():
            if not self.checkMessage(message, conf.name):
                continue
            client: Telegram = self.get_client(conf.name)
            if client:
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=message.userid, link=message.link)

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
            client: Telegram = self.get_client(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=message.userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict]):
        """
        注册命令，实现这个函数接收系统可用的命令菜单
        :param commands: 命令字典
        """
        for client in self._clients.values():
            client.register_commands(commands)
