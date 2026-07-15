import copy
import json
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, unquote

from app.core.context import MediaInfo, Context
from app.core.event import eventmanager
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.schemas import (
    CommandRegisterEventData,
    CommingMessage,
    MessageChannel,
    MessageResponse,
    Notification,
)
from app.schemas.types import ChainEventType, ModuleType
from app.utils.http import RequestUtils
from app.utils.structures import DictUtils

try:
    from app.modules.discord.discord import Discord
except Exception as err:  # ImportError or other load issues
    Discord = None
    logger.error(f"Discord 模块未加载，缺少依赖或初始化错误：{err}")


class DiscordModule(_ModuleBase, _MessageBase[Discord]):
    _IMAGE_SUFFIXES = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".svg",
    )
    _AUDIO_SUFFIXES = (
        ".mp3",
        ".m4a",
        ".wav",
        ".ogg",
        ".oga",
        ".opus",
        ".aac",
        ".amr",
        ".flac",
        ".mpga",
        ".mpeg",
        ".webm",
    )

    def init_module(self) -> None:
        """
        初始化模块
        """
        if not Discord:
            logger.error("Discord 依赖未就绪（需要安装 discord.py==2.6.4），模块未启动")
            return
        super().init_service(
            service_name=Discord.__name__.lower(), service_type=Discord
        )
        self._channel = MessageChannel.Discord

    @staticmethod
    def get_name() -> str:
        return "Discord"

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
        return MessageChannel.Discord

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止Discord模块实例失败：{err}")

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state = client.get_state()
            if not state:
                return False, f"Discord {name} Bot 未就绪"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def _get_admins(config: Optional[dict]) -> List[str]:
        """
        解析 Discord 管理员配置，兼容逗号分隔和首尾空白。
        """
        return [
            admin.strip()
            for admin in str((config or {}).get("DISCORD_ADMINS") or "").split(",")
            if admin.strip()
        ]

    @classmethod
    def _should_reject_admin_command(
            cls,
            config: Optional[dict],
            *user_ids: Optional[Union[str, int]],
    ) -> bool:
        """
        判断 Discord 命令或命令型按钮回调是否应因非管理员身份被拒绝。
        """
        admins = cls._get_admins(config)
        if not admins:
            return False
        candidates = [
            str(user_id).strip()
            for user_id in user_ids
            if user_id is not None and str(user_id).strip()
        ]
        return not any(candidate in admins for candidate in candidates)

    @staticmethod
    def _send_admin_denied(
            client: Optional[Discord],
            userid: Optional[Union[str, int]],
            chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        向 Discord 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(
                title="只有管理员才有权限执行此命令",
                userid=str(userid),
                original_chat_id=str(chat_id) if chat_id else None,
            )

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
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Discord = self.get_instance(client_config.name)
        try:
            msg_json: dict = json.loads(body)
        except Exception as e:
            logger.debug(f"解析 Discord 消息失败：{str(e)}")
            return None

        if not msg_json:
            return None

        msg_type = msg_json.get("type")
        userid = msg_json.get("userid")
        username = msg_json.get("username")

        if msg_type == "interaction":
            callback_data = msg_json.get("callback_data")
            message_id = msg_json.get("message_id")
            chat_id = msg_json.get("chat_id")
            if callback_data and userid:
                if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid, username
                ):
                    self._send_admin_denied(client, userid, chat_id)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的 Discord 按钮回调："
                    f"userid={userid}, username={username}, callback_data={callback_data}"
                )
                return CommingMessage(
                    channel=MessageChannel.Discord,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    text=f"CALLBACK:{callback_data}",
                    is_callback=True,
                    callback_data=callback_data,
                    message_id=message_id,
                    chat_id=str(chat_id) if chat_id else None,
                )
            return None

        if msg_type == "message":
            text = msg_json.get("text")
            chat_id = msg_json.get("chat_id")
            images = self._extract_images(msg_json)
            audio_refs = self._extract_audio_refs(msg_json)
            files = self._extract_files(msg_json)
            if (text or images or audio_refs or files) and userid:
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid, username
                ):
                    self._send_admin_denied(client, userid, chat_id)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的 Discord 消息："
                    f"userid={userid}, username={username}, text={text}, "
                    f"images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                    f"files={len(files) if files else 0}"
                )
                return CommingMessage(
                    channel=MessageChannel.Discord,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    text=text,
                    chat_id=str(chat_id) if chat_id else None,
                    images=images,
                    audio_refs=audio_refs,
                    files=files,
                )
        return None

    @staticmethod
    def _extract_images(
        msg_json: dict,
    ) -> Optional[List[CommingMessage.MessageImage]]:
        """
        从Discord消息中提取图片URL
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None
        images = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            if (
                attachment.get("type") == "image"
                or content_type.startswith("image/")
                or filename.endswith(DiscordModule._IMAGE_SUFFIXES)
            ):
                images.append(
                    CommingMessage.MessageImage(
                        ref=url,
                        name=attachment.get("filename"),
                        mime_type=attachment.get("content_type"),
                        size=attachment.get("size"),
                    )
                )
        return images if images else None

    @classmethod
    def _extract_audio_refs(cls, msg_json: dict) -> Optional[List[str]]:
        """
        从Discord消息中提取音频URL
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None
        audio_refs = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            if content_type.startswith("audio/") or filename.endswith(cls._AUDIO_SUFFIXES):
                audio_refs.append(f"discord://file/{quote(url, safe='')}")
        return audio_refs if audio_refs else None

    @classmethod
    def _extract_files(
        cls, msg_json: dict
    ) -> Optional[List[CommingMessage.MessageAttachment]]:
        """
        从 Discord 消息中提取非图片/非音频文件。
        """
        attachments = msg_json.get("attachments", [])
        if not attachments:
            return None

        files = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("proxy_url")
            if not url:
                continue
            content_type = (attachment.get("content_type") or "").lower()
            filename = (attachment.get("filename") or "").lower()
            is_image = (
                attachment.get("type") == "image"
                or content_type.startswith("image/")
                or filename.endswith(cls._IMAGE_SUFFIXES)
            )
            is_audio = content_type.startswith("audio/") or filename.endswith(
                cls._AUDIO_SUFFIXES
            )
            if is_image or is_audio:
                continue
            files.append(
                CommingMessage.MessageAttachment(
                    ref=f"discord://file/{quote(url, safe='')}",
                    name=attachment.get("filename"),
                    mime_type=attachment.get("content_type"),
                    size=attachment.get("size"),
                )
            )
        return files or None

    def download_discord_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载Discord附件并返回原始字节
        """
        if not file_ref or not file_ref.startswith("discord://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("discord://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and resp.content:
            return resp.content
        return None

    def post_message(self, message: Notification, **kwargs) -> None:
        """
        发送通知消息
        :param message: 消息通知对象
        """
        # DEBUG: Log entry and configs
        configs = self.get_configs()
        logger.debug(
            f"[Discord] post_message 被调用，message.source={message.source}, "
            f"message.userid={message.userid}, message.channel={message.channel}"
        )
        logger.debug(
            f"[Discord] 当前配置数量: {len(configs)}, 配置名称: {list(configs.keys())}"
        )
        logger.debug(
            f"[Discord] 当前实例数量: {len(self.get_instances())}, 实例名称: {list(self.get_instances().keys())}"
        )

        if not configs:
            logger.debug("[Discord] get_configs() 返回空，没有可用的 Discord 配置")
            return

        for conf in configs.values():
            logger.debug(
                f"[Discord] 检查配置: name={conf.name}, type={conf.type}, enabled={conf.enabled}"
            )
            if not self.check_message(message, conf.name):
                logger.debug(
                    f"[Discord] check_message 返回 False，跳过配置: {conf.name}"
                )
                continue
            logger.debug(f"[Discord] check_message 通过，准备发送到: {conf.name}")
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("discord_userid")
                if not userid:
                    logger.warn("用户没有指定 Discord 用户ID，消息无法发送")
                    return
            client: Discord = self.get_instance(conf.name)
            logger.debug(
                f"[Discord] get_instance('{conf.name}') 返回: {client is not None}"
            )
            if client:
                logger.debug(
                    f"[Discord] 调用 client 发送, userid={userid}, title={message.title[:50] if message.title else None}..."
                )
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                        original_chat_id=message.original_chat_id,
                    )
                else:
                    result = client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=userid,
                        link=message.link,
                        buttons=message.buttons,
                        original_message_id=message.original_message_id,
                        original_chat_id=message.original_chat_id,
                        mtype=message.mtype,
                    )
                logger.debug(f"[Discord] send_msg 返回结果: {result}")
            else:
                logger.warning(
                    f"[Discord] 未找到配置 '{conf.name}' 对应的 Discord 客户端实例"
                )

    def post_medias_message(
        self, message: Notification, medias: List[MediaInfo]
    ) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息体
        :param medias: 媒体信息
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Discord = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    title=message.title,
                    medias=medias,
                    userid=message.userid,
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
        :param torrents: 种子信息
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Discord = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    title=message.title,
                    torrents=torrents,
                    userid=message.userid,
                    buttons=message.buttons,
                    original_message_id=message.original_message_id,
                    original_chat_id=message.original_chat_id,
                )

    def delete_message(
        self,
        channel: MessageChannel,
        source: str,
        message_id: str,
        chat_id: Optional[str] = None,
    ) -> Optional[bool]:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID（Slack中为时间戳）
        :param chat_id: 聊天ID（频道ID）
        :return: 删除是否成功
        """
        if channel != self._channel:
            return None
        success = False
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Discord = self.get_instance(conf.name)
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
        buttons: Optional[List[List[dict]]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[bool]:
        """
        编辑消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :param buttons: 新的按钮列表
        :return: 编辑是否成功
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Discord = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=title or "",
                    text=text,
                    buttons=buttons,
                    original_message_id=message_id,
                    original_chat_id=str(chat_id),
                )
                if result and isinstance(result, tuple) and result[0]:
                    return True
                elif result:
                    return True
        return False

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        注册命令，实现这个函数接收系统可用的命令菜单。

        :param commands: 命令字典
        """
        for client_config in self.get_configs().values():
            client = self.get_instance(client_config.name)
            if not client:
                continue

            scoped_commands = copy.deepcopy(commands)
            event = eventmanager.send_event(
                ChainEventType.CommandRegister,
                CommandRegisterEventData(
                    commands=scoped_commands,
                    origin="Discord",
                    service=client_config.name,
                ),
            )

            if event and event.event_data:
                event_data: CommandRegisterEventData = event.event_data
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

            filtered_scoped_commands = DictUtils.filter_keys_to_subset(
                scoped_commands,
                commands,
            )
            if not filtered_scoped_commands:
                logger.debug("Filtered commands are empty, skipping registration.")
                client.delete_commands()
                continue
            if filtered_scoped_commands != commands:
                logger.debug(
                    f"Command set has changed, Updating new commands: {filtered_scoped_commands}"
                )
            client.register_commands(filtered_scoped_commands)

    def mark_message_processing_started(
        self,
        channel: MessageChannel,
        source: str,
        userid: Optional[Union[str, int]] = None,
        message_id: Optional[Union[str, int]] = None,
        chat_id: Optional[Union[str, int]] = None,
        text: Optional[str] = None,
    ) -> Optional[dict]:
        """
        使用 Discord typing 指示标记“正在处理”。
        """
        if channel != self._channel:
            return None
        if not text:
            return None
        config = self.get_config(source)
        if not config:
            return None
        client: Discord = self.get_instance(config.name)
        if not client:
            return None
        if not client.start_typing(
                userid=str(userid) if userid else None,
                chat_id=str(chat_id) if chat_id else None,
        ):
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": str(message_id) if message_id else None,
            "chat_id": str(chat_id) if chat_id else None,
            "metadata": {"kind": "typing"},
        }

    def mark_message_processing_finished(
        self,
        channel: MessageChannel,
        source: str,
        userid: Optional[Union[str, int]] = None,
        message_id: Optional[Union[str, int]] = None,
        chat_id: Optional[Union[str, int]] = None,
        status: Optional[dict] = None,
    ) -> Optional[bool]:
        """
        停止 Discord typing 续发任务。
        """
        if channel != self._channel:
            return None
        target_chat_id = (status or {}).get("chat_id") or chat_id
        target_userid = (status or {}).get("userid") or userid
        config = self.get_config(source)
        if not config:
            return False
        client: Discord = self.get_instance(config.name)
        if not client:
            return False
        return client.stop_typing(
            userid=str(target_userid) if target_userid else None,
            chat_id=str(target_chat_id) if target_chat_id else None,
        )

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
                userid = targets.get("discord_userid")
                if not userid:
                    logger.warn("用户没有指定 Discord 用户ID，消息无法发送")
                    return None
            client: Discord = self.get_instance(conf.name)
            if client:
                if message.file_path:
                    result = client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                    )
                else:
                    result = client.send_msg(
                        title=message.title or "",
                        text=message.text,
                        userid=userid,
                    )
                if result:
                    success, response_data = (
                        (result[0], result[1])
                        if isinstance(result, tuple)
                        else (result, None)
                    )
                    if success:
                        message_id = None
                        chat_id = None
                        if isinstance(response_data, dict):
                            message_id = response_data.get("message_id")
                            chat_id = response_data.get("chat_id")
                        elif response_data is not None:
                            message_id = str(response_data)
                        return MessageResponse(
                            message_id=str(message_id) if message_id else None,
                            chat_id=str(chat_id) if chat_id else None,
                            channel=MessageChannel.Discord,
                            source=conf.name,
                            success=True,
                        )
        return None
