import copy
import json
from typing import Dict
from typing import Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.core.event import Event
from app.core.event import eventmanager
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.telegram.telegram import Telegram
from app.schemas import MessageChannel, CommingMessage, Notification, CommandRegisterEventData, ConfigChangeEventData, \
    NotificationConf
from app.schemas.types import ModuleType, ChainEventType, SystemConfigKey, EventType
from app.utils.structures import DictUtils


class TelegramModule(_ModuleBase, _MessageBase[Telegram]):

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=Telegram.__name__.lower(),
                             service_type=Telegram)
        self._channel = MessageChannel.Telegram

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in [SystemConfigKey.Notifications.value]:
            return
        self.init_module()

    @staticmethod
    def get_name() -> str:
        return "Telegram"

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
        return MessageChannel.Telegram

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def stop(self):
        """
        停止模块
        """
        for client in self.get_instances().values():
            client.stop()

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
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
            普通消息格式：
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
            
            按钮回调格式：
            {
                'callback_query': {
                    'id': '',
                    'from': {...},
                    'message': {...},
                    'data': 'callback_data'
                }
            }
        """
        # 获取服务配置
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Telegram = self.get_instance(client_config.name)
        try:
            message: dict = json.loads(body)
        except Exception as err:
            logger.debug(f"解析Telegram消息失败：{str(err)}")
            return None

        if message:
            # 处理按钮回调
            if "callback_query" in message:
                return self._handle_callback_query(message, client_config)

            # 处理普通消息
            return self._handle_text_message(message, client_config, client)

        return None

    @staticmethod
    def _handle_callback_query(message: dict, client_config: NotificationConf) -> Optional[CommingMessage]:
        """
        处理按钮回调查询
        """
        callback_query = message.get("callback_query", {})
        user_info = callback_query.get("from", {})
        callback_data = callback_query.get("data", "")
        user_id = user_info.get("id")
        user_name = user_info.get("username")

        if callback_data and user_id:
            logger.info(f"收到来自 {client_config.name} 的Telegram按钮回调："
                        f"userid={user_id}, username={user_name}, callback_data={callback_data}")

            # 将callback_data作为特殊格式的text返回，以便主程序识别这是按钮回调
            callback_text = f"CALLBACK:{callback_data}"

            # 创建包含完整回调信息的CommingMessage
            return CommingMessage(
                channel=MessageChannel.Telegram,
                source=client_config.name,
                userid=user_id,
                username=user_name,
                text=callback_text,
                is_callback=True,
                callback_data=callback_data,
                message_id=callback_query.get("message", {}).get("message_id"),
                chat_id=str(callback_query.get("message", {}).get("chat", {}).get("id", "")),
                callback_query=callback_query
            )
        return None

    @staticmethod
    def _handle_text_message(msg: dict, client_config: NotificationConf, client: Telegram) -> Optional[CommingMessage]:
        """
        处理普通文本消息
        """
        text = msg.get("text")
        user_id = msg.get("from", {}).get("id")
        user_name = msg.get("from", {}).get("username")

        if text and user_id:
            logger.info(f"收到来自 {client_config.name} 的Telegram消息："
                        f"userid={user_id}, username={user_name}, text={text}")

            # 检查权限
            admin_users = client_config.config.get("TELEGRAM_ADMINS")
            user_list = client_config.config.get("TELEGRAM_USERS")
            chat_id = client_config.config.get("TELEGRAM_CHAT_ID")

            if text.startswith("/"):
                if admin_users \
                        and str(user_id) not in admin_users.split(',') \
                        and str(user_id) != chat_id:
                    client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                    return None
            else:
                if user_list \
                        and str(user_id) not in user_list.split(','):
                    logger.info(f"用户{user_id}不在用户白名单中，无法使用此机器人")
                    client.send_msg(title="你不在用户白名单中，无法使用此机器人", userid=user_id)
                    return None

            return CommingMessage(
                channel=MessageChannel.Telegram,
                source=client_config.name,
                userid=user_id,
                username=user_name,
                text=text
            )
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
                userid = targets.get('telegram_userid')
                if not userid:
                    logger.warn(f"用户没有指定 Telegram用户ID，消息无法发送")
                    return
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, text=message.text,
                                image=message.image, userid=userid, link=message.link,
                                buttons=message.buttons)

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
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=message.userid, link=message.link,
                                       buttons=message.buttons)

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
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=message.userid, link=message.link,
                                         buttons=message.buttons)

    def register_commands(self, commands: Dict[str, dict]):
        """
        注册命令，实现这个函数接收系统可用的命令菜单
        :param commands: 命令字典
        """
        for client_config in self.get_configs().values():
            client = self.get_instance(client_config.name)
            if not client:
                continue

            # 触发事件，允许调整命令数据，这里需要进行深复制，避免实例共享
            scoped_commands = copy.deepcopy(commands)
            event = eventmanager.send_event(
                ChainEventType.CommandRegister,
                CommandRegisterEventData(commands=scoped_commands, origin="Telegram", service=client_config.name)
            )

            # 如果事件返回有效的 event_data，使用事件中调整后的命令
            if event and event.event_data:
                event_data: CommandRegisterEventData = event.event_data
                # 如果事件被取消，跳过命令注册，并清理菜单
                if event_data.cancel:
                    client.delete_commands()
                    logger.debug(
                        f"Command registration for {client_config.name} canceled by event: {event_data.source}"
                    )
                    continue
                scoped_commands = event_data.commands or {}
                if not scoped_commands:
                    logger.debug("Filtered commands are empty, skipping registration.")
                    client.delete_commands()

            # scoped_commands 必须是 commands 的子集
            filtered_scoped_commands = DictUtils.filter_keys_to_subset(scoped_commands, commands)
            # 如果 filtered_scoped_commands 为空，则跳过注册
            if not filtered_scoped_commands:
                logger.debug("Filtered commands are empty, skipping registration.")
                client.delete_commands()
                continue
            # 对比调整后的命令与当前命令
            if filtered_scoped_commands != commands:
                logger.debug(f"Command set has changed, Updating new commands: {filtered_scoped_commands}")
            client.register_commands(filtered_scoped_commands)
