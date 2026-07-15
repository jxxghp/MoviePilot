import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, unquote

from app.core.context import MediaInfo, Context
from app.core.event import eventmanager
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.slack.slack import Slack
from app.schemas import (
    CommandRegisterEventData,
    CommingMessage,
    MessageChannel,
    MessageResponse,
    Notification,
)
from app.schemas.types import ChainEventType, ModuleType
from app.utils.structures import DictUtils


class SlackModule(_ModuleBase, _MessageBase[Slack]):
    PROCESSING_REACTION = "eyes"
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
        super().init_service(service_name=Slack.__name__.lower(), service_type=Slack)
        self._channel = MessageChannel.Slack

    @staticmethod
    def get_name() -> str:
        return "Slack"

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
        return MessageChannel.Slack

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止Slack模块实例失败：{err}")

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state = client.get_state()
            if not state:
                return False, f"Slack {name} 未就绪"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def _get_admins(config: Optional[dict]) -> List[str]:
        """
        解析 Slack 管理员配置，兼容逗号分隔和首尾空白。
        """
        return [
            admin.strip()
            for admin in str((config or {}).get("SLACK_ADMINS") or "").split(",")
            if admin.strip()
        ]

    @classmethod
    def _should_reject_admin_command(
            cls,
            config: Optional[dict],
            *user_ids: Optional[Union[str, int]],
    ) -> bool:
        """
        判断 Slack 命令或命令型按钮回调是否应因非管理员身份被拒绝。
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
    def _send_admin_denied(client: Optional[Slack], userid: Optional[Union[str, int]]) -> None:
        """
        向 Slack 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

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
        # 消息
        {
            'client_msg_id': '',
            'type': 'message',
            'text': 'hello',
            'user': '',
            'ts': '1670143568.444289',
            'blocks': [{
                'type': 'rich_text',
                'block_id': 'i2j+',
                'elements': [{
                    'type': 'rich_text_section',
                    'elements': [{
                        'type': 'text',
                        'text': 'hello'
                    }]
                }]
            }],
            'team': '',
            'client': '',
            'event_ts': '1670143568.444289',
            'channel_type': 'im'
        }
        # 命令
        {
          "token": "",
          "team_id": "",
          "team_domain": "",
          "channel_id": "",
          "channel_name": "directmessage",
          "user_id": "",
          "user_name": "",
          "command": "/subscribes",
          "text": "",
          "api_app_id": "",
          "is_enterprise_install": "false",
          "response_url": "",
          "trigger_id": ""
        }
        # 快捷方式
        {
          "type": "shortcut",
          "token": "XXXXXXXXXXXXX",
          "action_ts": "1581106241.371594",
          "team": {
            "id": "TXXXXXXXX",
            "domain": "shortcuts-test"
          },
          "user": {
            "id": "UXXXXXXXXX",
            "username": "aman",
            "team_id": "TXXXXXXXX"
          },
          "callback_id": "shortcut_create_task",
          "trigger_id": "944799105734.773906753841.38b5894552bdd4a780554ee59d1f3638"
        }
        # 按钮点击
        {
          "type": "block_actions",
          "team": {
            "id": "T9TK3CUKW",
            "domain": "example"
          },
          "user": {
            "id": "UA8RXUSPL",
            "username": "jtorrance",
            "team_id": "T9TK3CUKW"
          },
          "api_app_id": "AABA1ABCD",
          "token": "9s8d9as89d8as9d8as989",
          "container": {
            "type": "message_attachment",
            "message_ts": "1548261231.000200",
            "attachment_id": 1,
            "channel_id": "CBR2V3XEX",
            "is_ephemeral": false,
            "is_app_unfurl": false
          },
          "trigger_id": "12321423423.333649436676.d8c1bb837935619ccad0f624c448ffb3",
          "client": {
            "id": "CBR2V3XEX",
            "name": "review-updates"
          },
          "message": {
            "bot_id": "BAH5CA16Z",
            "type": "message",
            "text": "This content can't be displayed.",
            "user": "UAJ2RU415",
            "ts": "1548261231.000200",
            ...
          },
          "response_url": "https://hooks.slack.com/actions/AABA1ABCD/1232321423432/D09sSasdasdAS9091209",
          "actions": [
            {
              "action_id": "WaXA",
              "block_id": "=qXel",
              "text": {
                "type": "plain_text",
                "text": "View",
                "emoji": true
              },
              "value": "click_me_123",
              "type": "button",
              "action_ts": "1548426417.840180"
            }
          ]
        }
        """
        # 获取服务配置
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Slack = self.get_instance(client_config.name)
        try:
            msg_json = json.loads(body)
            while isinstance(msg_json, str):
                msg_json = json.loads(msg_json)
        except Exception as err:
            logger.debug(f"解析Slack消息失败：{str(err)}")
            return None
        if not isinstance(msg_json, dict):
            logger.debug(f"Slack消息格式无效：{type(msg_json)}")
            return None
        if msg_json:
            images = None
            audio_refs = None
            files = None
            message_id = None
            chat_id = None
            if msg_json.get("type") == "message":
                userid = msg_json.get("user")
                text = msg_json.get("text")
                username = msg_json.get("user")
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid, username
                ):
                    self._send_admin_denied(client, userid)
                    return None
                message_id = msg_json.get("ts")
                chat_id = msg_json.get("channel")
                images = self._extract_images(msg_json)
                audio_refs = self._extract_audio_refs(msg_json)
                files = self._extract_files(msg_json)
            elif msg_json.get("type") == "block_actions":
                userid = msg_json.get("user", {}).get("id")
                callback_data = msg_json.get("actions")[0].get("value")
                # 使用CALLBACK前缀标识按钮回调
                text = f"CALLBACK:{callback_data}"
                username = msg_json.get("user", {}).get("name")
                if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid, username
                ):
                    self._send_admin_denied(client, userid)
                    return None

                # 获取原消息信息用于编辑
                message_info = msg_json.get("message", {})
                # Slack消息的时间戳作为消息ID
                message_ts = message_info.get("ts")
                channel_id = msg_json.get("channel", {}).get("id") or msg_json.get(
                    "container", {}
                ).get("channel_id")

                logger.info(
                    f"收到来自 {client_config.name} 的Slack按钮回调："
                    f"userid={userid}, username={username}, callback_data={callback_data}"
                )

                # 创建包含回调信息的CommingMessage
                return CommingMessage(
                    channel=MessageChannel.Slack,
                    source=client_config.name,
                    userid=userid,
                    username=username,
                    text=text,
                    is_callback=True,
                    callback_data=callback_data,
                    message_id=message_ts,
                    chat_id=channel_id,
                )
            elif msg_json.get("type") == "event_callback":
                userid = msg_json.get("event", {}).get("user")
                text = re.sub(
                    r"<@[0-9A-Z]+>",
                    "",
                    msg_json.get("event", {}).get("text"),
                    flags=re.IGNORECASE,
                ).strip()
                username = ""
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
                message_id = msg_json.get("event", {}).get("ts")
                chat_id = msg_json.get("event", {}).get("channel")
                images = self._extract_images(msg_json.get("event", {}))
                audio_refs = self._extract_audio_refs(msg_json.get("event", {}))
                files = self._extract_files(msg_json.get("event", {}))
            elif msg_json.get("type") == "shortcut":
                userid = msg_json.get("user", {}).get("id")
                text = msg_json.get("callback_id")
                username = msg_json.get("user", {}).get("username")
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, userid, username
                ):
                    self._send_admin_denied(client, userid)
                    return None
            elif msg_json.get("command"):
                userid = msg_json.get("user_id")
                text = msg_json.get("command")
                username = msg_json.get("user_name")
                chat_id = msg_json.get("channel_id")
                if self._should_reject_admin_command(client_config.config, userid, username):
                    self._send_admin_denied(client, userid)
                    return None
            else:
                return None
            logger.info(
                f"收到来自 {client_config.name} 的Slack消息：userid={userid}, username={username}, "
                f"text={text}, images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}, "
                f"files={len(files) if files else 0}"
            )
            return CommingMessage(
                channel=MessageChannel.Slack,
                source=client_config.name,
                userid=userid,
                username=username,
                text=text,
                message_id=message_id,
                chat_id=chat_id,
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
        从Slack消息中提取图片URL
        """
        files = msg_json.get("files", [])
        if not files:
            return None
        images = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = str(file.get("filetype", "")).lower()
            mime_type = str(file.get("mimetype", "")).lower()
            if (
                file_type == "image"
                or file_ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp")
                or mime_type.startswith("image/")
            ):
                url = file.get("url_private") or file.get("url_private_download")
                if url:
                    images.append(
                        CommingMessage.MessageImage(
                            ref=url,
                            name=file.get("name") or file.get("title"),
                            mime_type=file.get("mimetype"),
                            size=file.get("size"),
                        )
                    )
        return images if images else None

    @classmethod
    def _extract_audio_refs(cls, msg_json: dict) -> Optional[List[str]]:
        """
        从Slack消息中提取音频文件引用
        """
        files = msg_json.get("files", [])
        if not files:
            return None
        audio_refs = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = f".{str(file.get('filetype', '')).lower().lstrip('.')}"
            mime_type = str(file.get("mimetype", "")).lower()
            if (
                file_type == "audio"
                or mime_type.startswith("audio/")
                or file_ext in cls._AUDIO_SUFFIXES
            ):
                url = file.get("url_private_download") or file.get("url_private")
                if url:
                    audio_refs.append(f"slack://file/{quote(url, safe='')}")
        return audio_refs if audio_refs else None

    @classmethod
    def _extract_files(
        cls, msg_json: dict
    ) -> Optional[List[CommingMessage.MessageAttachment]]:
        """
        从 Slack 消息中提取非图片/非音频文件。
        """
        files = msg_json.get("files", [])
        if not files:
            return None

        attachments = []
        for file in files:
            file_type = str(file.get("type", "")).lower()
            file_ext = f".{str(file.get('filetype', '')).lower().lstrip('.')}"
            mime_type = str(file.get("mimetype", "")).lower()
            is_image = (
                file_type == "image"
                or file_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
                or mime_type.startswith("image/")
            )
            is_audio = (
                file_type == "audio"
                or mime_type.startswith("audio/")
                or file_ext in cls._AUDIO_SUFFIXES
            )
            if is_image or is_audio:
                continue

            url = file.get("url_private_download") or file.get("url_private")
            if not url:
                continue
            attachments.append(
                CommingMessage.MessageAttachment(
                    ref=f"slack://file/{quote(url, safe='')}",
                    name=file.get("name") or file.get("title"),
                    mime_type=file.get("mimetype"),
                    size=file.get("size"),
                )
            )
        return attachments or None

    def download_slack_file_to_data_url(self, file_url: str, source: str) -> Optional[str]:
        """
        下载Slack文件并转为data URL
        :param file_url: Slack私有文件URL
        :param source: 来源名称
        :return: data URL
        """
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_data = client.download_file(file_url)
        if file_data:
            import base64

            content, mime_type = file_data
            return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
        return None

    def download_slack_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载Slack音频文件并返回原始字节
        """
        if not file_ref or not file_ref.startswith("slack://file/"):
            return None
        config = self.get_config(source)
        if not config:
            return None
        client = self.get_instance(config.name)
        if not client:
            return None
        file_url = unquote(file_ref.replace("slack://file/", "", 1))
        file_data = client.download_file(file_url)
        if file_data:
            content, _ = file_data
            return content
        return None

    def post_message(self, message: Notification, **kwargs) -> None:
        """
        发送消息
        :param message: 消息
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("slack_userid")
                if not userid:
                    logger.warn(f"用户没有指定 Slack用户ID，消息无法发送")
                    return
            client: Slack = self.get_instance(conf.name)
            if client:
                if message.file_path:
                    client.send_file(
                        file_path=message.file_path,
                        file_name=message.file_name,
                        title=message.title,
                        text=message.text,
                        userid=userid,
                    )
                else:
                    client.send_msg(
                        title=message.title,
                        text=message.text,
                        image=message.image,
                        userid=userid,
                        link=message.link,
                        buttons=message.buttons,
                        original_message_id=message.original_message_id,
                        original_chat_id=message.original_chat_id,
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
            client: Slack = self.get_instance(conf.name)
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
            client: Slack = self.get_instance(conf.name)
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
            client: Slack = self.get_instance(conf.name)
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
            client: Slack = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=title or "",
                    text=text,
                    buttons=buttons,
                    original_message_id=str(message_id),
                    original_chat_id=str(chat_id),
                )
                if result and result[0]:
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
                    origin="Slack",
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
        使用 Slack reaction 标记“正在处理”。
        """
        if channel != self._channel:
            return None
        if not message_id or not chat_id or not text or str(text).startswith("CALLBACK:"):
            return None
        config = self.get_config(source)
        if not config:
            return None
        client: Slack = self.get_instance(config.name)
        if not client:
            return None
        if not client.add_reaction(
                channel=str(chat_id),
                timestamp=str(message_id),
                emoji=self.PROCESSING_REACTION,
        ):
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": str(message_id),
            "chat_id": str(chat_id),
            "metadata": {
                "kind": "reaction",
                "emoji": self.PROCESSING_REACTION,
            },
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
        移除 Slack “正在处理” reaction。
        """
        if channel != self._channel:
            return None
        metadata = (status or {}).get("metadata") or {}
        target_message_id = (status or {}).get("message_id") or message_id
        target_chat_id = (status or {}).get("chat_id") or chat_id
        emoji = metadata.get("emoji") or self.PROCESSING_REACTION
        if not target_message_id or not target_chat_id:
            return False
        config = self.get_config(source)
        if not config:
            return False
        client: Slack = self.get_instance(config.name)
        if not client:
            return False
        return client.remove_reaction(
            channel=str(target_chat_id),
            timestamp=str(target_message_id),
            emoji=str(emoji),
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
                userid = targets.get("slack_userid")
                if not userid:
                    logger.warn("用户没有指定 Slack 用户ID，消息无法发送")
                    return None
            client: Slack = self.get_instance(conf.name)
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
                if result and result[0]:
                    # Slack 使用时间戳作为 message_id，chat_id 是频道ID
                    # 注意：这里返回的是发送后的结果，需要获取实际的 message_id
                    # 由于 Slack API 返回的是 result[1]，包含完整响应，我们需要从中提取
                    response_data = result[1]
                    message_id = None
                    channel_id = None
                    if hasattr(response_data, "get"):
                        message_id = response_data.get("ts")
                        channel_id = response_data.get("channel")
                    if not message_id and hasattr(response_data, "data"):
                        files = (response_data.data or {}).get("files") or []
                        if files:
                            message_id = files[0].get("id")
                            shares = (
                                files[0].get("shares", {})
                                .get("private", {})
                            )
                            if shares:
                                channel_id = next(iter(shares.keys()), None)
                    return MessageResponse(
                        message_id=message_id,
                        chat_id=channel_id,
                        channel=MessageChannel.Slack,
                        source=conf.name,
                        success=True,
                    )
        return None
