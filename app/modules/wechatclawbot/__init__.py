import json
from typing import Any, List, Optional, Tuple, Union

from app.core.cache import TTLCache
from app.core.context import Context, MediaInfo
from app.log import logger
from app.modules import _MessageBase, _ModuleBase
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.schemas import CommingMessage, Notification
from app.schemas.types import MessageChannel, ModuleType


class WechatClawBotModule(_ModuleBase, _MessageBase[WechatClawBot]):
    def __init__(self):
        """初始化模块级去重缓存，拦截 iLink 偶发的重复回放消息。"""
        super().__init__()
        # iLink 偶发会重复回放同一条 update，这里按 message_id 做渠道内幂等保护。
        self._recent_message_ids = TTLCache(
            region="wechatclawbot_message_dedup",
            maxsize=8192,
            ttl=7 * 24 * 60 * 60,
        )

    def init_module(self) -> None:
        """初始化模块。"""
        self.stop()
        super().init_service(
            service_name=WechatClawBot.__name__.lower(), service_type=WechatClawBot
        )
        self._channel = MessageChannel.WechatClawBot

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "微信 ClawBot"

    @staticmethod
    def get_type() -> ModuleType:
        """获取模块类型。"""
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> MessageChannel:
        """获取模块子类型。"""
        return MessageChannel.WechatClawBot

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级。"""
        return 2

    def stop(self):
        """停止模块。"""
        for client in self.get_instances().values():
            if hasattr(client, "stop"):
                try:
                    client.stop()
                except Exception as err:
                    logger.error(f"停止微信 ClawBot 模块实例失败：{err}")

    def test(self) -> Optional[Tuple[bool, str]]:
        """测试模块连接性。"""
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state, message = client.test_connection()
            if not state:
                return False, f"微信 ClawBot {name} 未就绪：{message}"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """初始化模块设置。"""
        pass

    @staticmethod
    def _load_json(body: Any) -> Optional[dict]:
        """将内容解析为 JSON 字典。"""
        if isinstance(body, dict):
            payload = body
        elif isinstance(body, bytes):
            payload = json.loads(body.decode("utf-8", errors="ignore"))
        else:
            payload = json.loads(body)
        while isinstance(payload, str):
            payload = json.loads(payload)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _normalize_audio_refs(audio_refs: Any) -> Optional[List[str]]:
        """标准化音频引用列表。"""
        if not audio_refs:
            return None
        if not isinstance(audio_refs, list):
            audio_refs = [audio_refs]
        normalized = [str(item).strip() for item in audio_refs if str(item).strip()]
        return normalized or None

    @staticmethod
    def _normalize_files(files: Any) -> Optional[List[CommingMessage.MessageAttachment]]:
        """标准化文件附件列表。"""
        if not files:
            return None
        if not isinstance(files, list):
            files = [files]
        normalized = []
        for item in files:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref") or item.get("url") or item.get("file_url")
            if not ref:
                continue
            size = item.get("size")
            try:
                size = int(size) if size is not None else None
            except (TypeError, ValueError):
                size = None
            normalized.append(
                CommingMessage.MessageAttachment(
                    ref=ref,
                    name=item.get("name") or item.get("filename"),
                    mime_type=item.get("mime_type") or item.get("content_type"),
                    size=size,
                )
            )
        return normalized or None

    def _is_duplicate_message(
        self, source: str, message_id: Optional[Union[str, int]]
    ) -> bool:
        """按渠道名和消息ID判断是否重复，避免重复回放再次进入业务链路。"""
        if message_id in (None, ""):
            return False
        cache_key = f"{source}:{message_id}"
        if self._recent_message_ids.exists(cache_key):
            return True
        self._recent_message_ids.set(cache_key, True)
        return False

    def message_parser(
        self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[CommingMessage]:
        """解析微信 ClawBot 转发到消息入口的 JSON 报文。"""
        client_config = self.get_config(source)
        if not client_config:
            return None
        try:
            message = self._load_json(body)
        except Exception as err:
            logger.debug(f"解析微信 ClawBot 消息失败：{err}")
            return None

        if not message:
            return None
        channel_name = (message.get("__channel__") or "").strip().lower()
        if channel_name and channel_name != "wechatclawbot":
            return None

        user_id = str(message.get("userid") or "").strip()
        if not user_id:
            return None

        message_id = message.get("message_id")
        text = str(message.get("text") or "").strip()
        username = str(message.get("username") or user_id).strip() or user_id
        images = CommingMessage.MessageImage.normalize_list(message.get("images"))
        audio_refs = self._normalize_audio_refs(message.get("audio_refs"))
        files = self._normalize_files(message.get("files"))
        if not text and not images and not audio_refs and not files:
            return None
        if self._is_duplicate_message(client_config.name, message_id):
            logger.info(
                "忽略重复的微信 ClawBot 消息：source=%s, userid=%s, message_id=%s",
                client_config.name,
                user_id,
                message_id,
            )
            return None

        admins = [
            admin.strip()
            for admin in str(client_config.config.get("WECHATCLAWBOT_ADMINS") or "").split(",")
            if admin.strip()
        ]
        if text.startswith("/") and admins and user_id not in admins:
            client = self.get_instance(client_config.name)
            if client:
                client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
            return None

        logger.info(
            f"收到来自 {client_config.name} 的微信 ClawBot 消息："
            f"userid={user_id}, message_id={message_id}, text={text}, "
            f"images={len(images) if images else 0}, "
            f"audios={len(audio_refs) if audio_refs else 0}, files={len(files) if files else 0}"
        )
        return CommingMessage(
            channel=MessageChannel.WechatClawBot,
            source=client_config.name,
            userid=user_id,
            username=username,
            text=text,
            message_id=message_id,
            chat_id=str(message.get("chat_id") or "") or None,
            images=images,
            audio_refs=audio_refs,
            files=files,
        )

    def post_message(self, message: Notification, **kwargs) -> None:
        """发送消息。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get("wechatclawbot_userid")
                if not userid:
                    logger.warning("用户没有指定 微信 ClawBot 用户ID，消息无法发送")
                    return
            client: WechatClawBot = self.get_instance(conf.name)
            if not client:
                continue
            if message.file_path:
                client.send_file(
                    file_path=message.file_path,
                    file_name=message.file_name,
                    title=message.title,
                    text=message.text,
                    userid=userid,
                )
            elif message.voice_path:
                client.send_file(
                    file_path=message.voice_path,
                    title=message.voice_caption or message.title,
                    text=message.text,
                    userid=userid,
                )
            else:
                client.send_msg(
                    title=message.title or "",
                    text=message.text,
                    image=message.image,
                    userid=userid,
                    link=message.link,
                )

    def download_wechat_image_to_data_url(
        self, image_ref: str, source: str
    ) -> Optional[str]:
        """下载微信 ClawBot 图片并转换为 data URL。"""
        if not image_ref or not image_ref.startswith("wxclaw://image/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        return client.download_image_to_data_url(image_ref)

    def download_wechat_media_bytes(
        self, media_ref: str, source: str
    ) -> Optional[bytes]:
        """下载微信 ClawBot 语音或文件附件。"""
        if not media_ref or not media_ref.startswith(("wxclaw://file/", "wxclaw://voice/")):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        return client.download_media_bytes(media_ref)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
        """发送媒体选择列表。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: WechatClawBot = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(medias=medias, userid=message.userid)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> None:
        """发送种子选择列表。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: WechatClawBot = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    torrents=torrents,
                    userid=message.userid,
                    title=message.title,
                    link=message.link,
                )
