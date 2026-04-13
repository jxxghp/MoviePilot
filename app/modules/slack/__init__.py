import json
import re
from urllib.parse import quote, unquote
from typing import Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.slack.slack import Slack
from app.schemas import MessageChannel, CommingMessage, Notification, MessageResponse
from app.schemas.types import ModuleType


class SlackModule(_ModuleBase, _MessageBase[Slack]):
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
                return False, f"Slack {name} 未就绪"
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
            if msg_json.get("type") == "message":
                userid = msg_json.get("user")
                text = msg_json.get("text")
                username = msg_json.get("user")
                images = self._extract_images(msg_json)
                audio_refs = self._extract_audio_refs(msg_json)
            elif msg_json.get("type") == "block_actions":
                userid = msg_json.get("user", {}).get("id")
                callback_data = msg_json.get("actions")[0].get("value")
                # 使用CALLBACK前缀标识按钮回调
                text = f"CALLBACK:{callback_data}"
                username = msg_json.get("user", {}).get("name")

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
                images = self._extract_images(msg_json.get("event", {}))
                audio_refs = self._extract_audio_refs(msg_json.get("event", {}))
            elif msg_json.get("type") == "shortcut":
                userid = msg_json.get("user", {}).get("id")
                text = msg_json.get("callback_id")
                username = msg_json.get("user", {}).get("username")
            elif msg_json.get("command"):
                userid = msg_json.get("user_id")
                text = msg_json.get("command")
                username = msg_json.get("user_name")
            else:
                return None
            logger.info(
                f"收到来自 {client_config.name} 的Slack消息：userid={userid}, username={username}, "
                f"text={text}, images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}"
            )
            return CommingMessage(
                channel=MessageChannel.Slack,
                source=client_config.name,
                userid=userid,
                username=username,
                text=text,
                images=images,
                audio_refs=audio_refs,
            )
        return None

    @staticmethod
    def _extract_images(msg_json: dict) -> Optional[List[str]]:
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
                    images.append(url)
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
    ) -> bool:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 指定的消息源
        :param message_id: 消息ID（Slack中为时间戳）
        :param chat_id: 聊天ID（频道ID）
        :return: 删除是否成功
        """
        success = False
        for conf in self.get_configs().values():
            if channel != self._channel:
                break
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
            client: Slack = self.get_instance(conf.name)
            if client:
                result = client.send_msg(
                    title=title or "",
                    text=text,
                    original_message_id=str(message_id),
                    original_chat_id=str(chat_id),
                )
                if result and result[0]:
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
                userid = targets.get("slack_userid")
                if not userid:
                    logger.warn("用户没有指定 Slack 用户ID，消息无法发送")
                    return None
            client: Slack = self.get_instance(conf.name)
            if client:
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
                    message_id = (
                        response_data.get("ts")
                        if isinstance(response_data, dict)
                        else None
                    )
                    channel_id = (
                        response_data.get("channel")
                        if isinstance(response_data, dict)
                        else None
                    )
                    return MessageResponse(
                        message_id=message_id,
                        chat_id=channel_id,
                        channel=MessageChannel.Slack,
                        source=conf.name,
                        success=True,
                    )
        return None
