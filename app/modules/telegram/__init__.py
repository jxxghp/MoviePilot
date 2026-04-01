import copy
import json
import re
from typing import Dict, Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.core.event import eventmanager
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.telegram.telegram import Telegram
from app.schemas import (
    MessageChannel,
    CommingMessage,
    Notification,
    CommandRegisterEventData,
    NotificationConf,
    MessageResponse,
)
from app.schemas.types import ModuleType, ChainEventType
from app.utils.structures import DictUtils


class TelegramModule(_ModuleBase, _MessageBase[Telegram]):
    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=Telegram.__name__.lower(), service_type=Telegram
        )
        self._channel = MessageChannel.Telegram

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
                return False, f"Telegram {name} 未就绪"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[CommingMessage]:
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
    def _handle_callback_query(
        message: dict, client_config: NotificationConf
    ) -> Optional[CommingMessage]:
        """
        处理按钮回调查询
        """
        callback_query = message.get("callback_query", {})
        user_info = callback_query.get("from", {})
        callback_data = callback_query.get("data", "")
        user_id = user_info.get("id")
        user_name = user_info.get("username")

        if callback_data and user_id:
            logger.info(
                f"收到来自 {client_config.name} 的Telegram按钮回调："
                f"userid={user_id}, username={user_name}, callback_data={callback_data}"
            )

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
                chat_id=str(
                    callback_query.get("message", {}).get("chat", {}).get("id", "")
                ),
                callback_query=callback_query,
            )
        return None

    def _handle_text_message(
        self, msg: dict, client_config: NotificationConf, client: Telegram
    ) -> Optional[CommingMessage]:
        """
        处理普通文本消息
        """
        text = msg.get("text") or msg.get("caption")
        user_id = msg.get("from", {}).get("id")
        user_name = msg.get("from", {}).get("username")
        chat_id = msg.get("chat", {}).get("id")

        # 将 text_link 实体中的 URL 嵌入到文本中
        if text:
            text = self._embed_entity_links(text, msg.get("entities") or msg.get("caption_entities"))

        # 将 reply_markup 中的 URL 按钮信息追加到文本中
        text = self._append_reply_markup_links(text, msg.get("reply_markup"))

        images = self._extract_images(msg)

        if user_id:
            if not text and not images:
                logger.debug(
                    f"收到来自 {client_config.name} 的Telegram消息无文本和图片"
                )
                return None

            logger.info(
                f"收到来自 {client_config.name} 的Telegram消息："
                f"userid={user_id}, username={user_name}, chat_id={chat_id}, text={text}, images={len(images) if images else 0}"
            )

            cleaned_text = (
                self._clean_bot_mention(text, client.bot_username if client else None)
                if text
                else None
            )

            admin_users = client_config.config.get("TELEGRAM_ADMINS")
            user_list = client_config.config.get("TELEGRAM_USERS")
            config_chat_id = client_config.config.get("TELEGRAM_CHAT_ID")

            if cleaned_text and cleaned_text.startswith("/"):
                if (
                    admin_users
                    and str(user_id) not in admin_users.split(",")
                    and str(user_id) != config_chat_id
                ):
                    client.send_msg(
                        title="只有管理员才有权限执行此命令", userid=user_id
                    )
                    return None
            else:
                if user_list and str(user_id) not in user_list.split(","):
                    logger.info(f"用户{user_id}不在用户白名单中，无法使用此机器人")
                    client.send_msg(
                        title="你不在用户白名单中，无法使用此机器人", userid=user_id
                    )
                    return None

            return CommingMessage(
                channel=MessageChannel.Telegram,
                source=client_config.name,
                userid=user_id,
                username=user_name,
                text=cleaned_text,
                chat_id=str(chat_id) if chat_id else None,
                images=images if images else None,
            )
        return None

    @staticmethod
    def _extract_images(msg: dict) -> Optional[List[str]]:
        """
        从Telegram消息中提取图片file_id
        """
        images = []
        photo = msg.get("photo")
        if photo and isinstance(photo, list):
            largest_photo = photo[-1]
            file_id = largest_photo.get("file_id")
            if file_id:
                images.append(file_id)

        document = msg.get("document")
        if document:
            file_id = document.get("file_id")
            mime_type = document.get("mime_type", "")
            if file_id and mime_type.startswith("image/"):
                images.append(file_id)

        return images if images else None

    @staticmethod
    def _embed_entity_links(text: str, entities: Optional[List[dict]]) -> str:
        """
        将 text_link 实体中的 URL 嵌入到文本中

        :param text: 原始文本
        :param entities: 消息实体列表
        :return: 嵌入链接后的文本
        """
        if not entities:
            return text
        text_link_entities = sorted(
            [e for e in entities if e.get("type") == "text_link" and e.get("url")],
            key=lambda e: e.get("offset", 0),
            reverse=True,
        )
        for entity in text_link_entities:
            offset = entity.get("offset", 0)
            length = entity.get("length", 0)
            url = entity["url"]
            display_text = text[offset: offset + length]
            text = text[:offset] + f"{display_text}({url})" + text[offset + length:]
        return text

    @staticmethod
    def _append_reply_markup_links(text: Optional[str], reply_markup: Optional[dict]) -> Optional[str]:
        """
        将 reply_markup 中的 URL 按钮信息追加到文本末尾

        :param text: 原始文本
        :param reply_markup: 消息的 reply_markup 字段
        :return: 追加按钮链接后的文本
        """
        if not reply_markup:
            return text
        inline_keyboard = reply_markup.get("inline_keyboard")
        if not inline_keyboard:
            return text
        button_lines = []
        for row in inline_keyboard:
            for button in row:
                btn_text = button.get("text", "")
                btn_url = button.get("url")
                if btn_url:
                    button_lines.append(f"{btn_text}({btn_url})")
        if not button_lines:
            return text
        buttons_text = "\n".join(button_lines)
        if text:
            return f"{text}\n{buttons_text}"
        return buttons_text

    @staticmethod
    def _clean_bot_mention(text: str, bot_username: Optional[str]) -> str:
        """
        清理消息中的@bot部分，确保文本处理一致性
        :param text: 原始消息文本
        :param bot_username: bot用户名
        :return: 清理后的文本
        """
        if not text or not bot_username:
            return text

        # Remove @bot_username from the beginning and any position in text
        cleaned = text
        mention_pattern = f"@{bot_username}"

        # Remove mention at the beginning with optional following space
        if cleaned.startswith(mention_pattern):
            cleaned = cleaned[len(mention_pattern):].lstrip()

        # Remove mention at any other position
        cleaned = cleaned.replace(mention_pattern, "").strip()

        # Clean up multiple spaces
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        return cleaned

    def post_message(self, message: Notification, **kwargs) -> None:
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
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn(f"用户没有指定 Telegram用户ID，消息无法发送")
                    return
            client: Telegram = self.get_instance(conf.name)
            if client:
                client.send_msg(
                    title=message.title,
                    text=message.text,
                    image=message.image,
                    userid=userid,
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                    disable_web_page_preview=message.disable_web_page_preview,
                )

    def post_medias_message(
        self, message: Notification, medias: List[MediaInfo]
    ) -> None:
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
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=message.userid,
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                )

    def post_torrents_message(
        self, message: Notification, torrents: List[Context]
    ) -> None:
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
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=message.userid,
                    link=message.link,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                )

    def delete_message(
        self,
        channel: MessageChannel,
        source: str,
        message_id: int,
        chat_id: Optional[int] = None,
    ) -> bool:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :return: 删除是否成功
        """
        success = False
        for conf in self.get_configs().values():
            if channel != self._channel:
                break
            if source != conf.name:
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                result = client.delete_msg(message_id=message_id, chat_id=chat_id)
                if result:
                    success = True
        return success

    def edit_message(
        self,
        channel: MessageChannel,
        source: str,
        message_id: Union[str, int],
        chat_id: Union[str, int],
        text: str,
        title: Optional[str] = None,
    ) -> bool:
        """
        编辑消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :return: 编辑是否成功
        """
        if channel != self._channel:
            return False
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Telegram = self.get_instance(conf.name)
            if client:
                result = client.edit_msg(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    title=title,
                )
                if result:
                    return True
        return False

    def send_direct_message(self, message: Notification) -> Optional[MessageResponse]:
        """
        直接发送消息并返回消息ID等信息
        :param message: 消息体
        :return: 消息响应（包含message_id, chat_id等）
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("telegram_userid")
                if not userid:
                    logger.warn("用户没有指定 Telegram用户ID，消息无法发送")
                    return None
            client: Telegram = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=message.title,
                    text=message.text,
                    image=message.image,
                    userid=userid,
                    link=message.link,
                    disable_web_page_preview=message.disable_web_page_preview,
                )
                if result and result.get("success"):
                    return MessageResponse(
                        message_id=result.get("message_id"),
                        chat_id=result.get("chat_id"),
                        channel=MessageChannel.Telegram,
                        source=conf.name,
                        success=True,
                    )
        return None

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
                CommandRegisterEventData(
                    commands=scoped_commands,
                    origin="Telegram",
                    service=client_config.name,
                ),
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
            filtered_scoped_commands = DictUtils.filter_keys_to_subset(
                scoped_commands, commands
            )
            # 如果 filtered_scoped_commands 为空，则跳过注册
            if not filtered_scoped_commands:
                logger.debug("Filtered commands are empty, skipping registration.")
                client.delete_commands()
                continue
            # 对比调整后的命令与当前命令
            if filtered_scoped_commands != commands:
                logger.debug(
                    f"Command set has changed, Updating new commands: {filtered_scoped_commands}"
                )
            client.register_commands(filtered_scoped_commands)

    def download_file_to_base64(self, file_id: str, source: str) -> Optional[str]:
        """
        下载Telegram文件并转为base64
        :param file_id: Telegram文件ID
        :param source: 来源名称
        :return: base64编码的图片数据
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_content = client.download_file(file_id)
        if file_content:
            import base64

            return base64.b64encode(file_content).decode()
        return None
