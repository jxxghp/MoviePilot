import json
from typing import Optional, Union, List, Tuple, Any
from urllib.parse import quote, unquote

from app.core.context import MediaInfo, Context
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.synologychat.synologychat import SynologyChat
from app.schemas import MessageChannel, CommingMessage, Notification
from app.schemas.types import ModuleType
from app.utils.http import RequestUtils


class SynologyChatModule(_ModuleBase, _MessageBase[SynologyChat]):
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
                return False, f"Synology Chat {name} 未就绪"
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
            images = self._extract_images(message)
            audio_refs = self._extract_audio_refs(message)
            if (text or images or audio_refs) and user_id:
                logger.info(
                    f"收到来自 {client_config.name} 的SynologyChat消息："
                    f"userid={user_id}, username={user_name}, text={text}, "
                    f"images={len(images) if images else 0}, audios={len(audio_refs) if audio_refs else 0}"
                )
                return CommingMessage(channel=MessageChannel.SynologyChat, source=client_config.name,
                                      userid=user_id, username=user_name, text=text or "",
                                      images=images, audio_refs=audio_refs)
        except Exception as err:
            logger.debug(f"解析SynologyChat消息失败：{str(err)}")
        return None

    @classmethod
    def _extract_images(cls, message: dict) -> Optional[List[str]]:
        images = []
        for key in ("file_url", "image_url", "pic_url"):
            value = message.get(key)
            if isinstance(value, str) and cls._looks_like_image(value):
                images.append(value)

        for key in ("attachments", "files"):
            raw_value = message.get(key)
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                parsed = raw_value
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, str) and cls._looks_like_image(item):
                    images.append(item)
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("file_url") or item.get("image_url")
                    if isinstance(url, str) and cls._looks_like_image(url):
                        images.append(url)

        deduped = []
        for image in images:
            if image not in deduped:
                deduped.append(image)
        return deduped or None

    @classmethod
    def _extract_audio_refs(cls, message: dict) -> Optional[List[str]]:
        audio_refs = []
        for key in ("audio_url", "voice_url", "file_url"):
            value = message.get(key)
            if isinstance(value, str) and cls._looks_like_audio(value):
                audio_refs.append(f"synology://file/{quote(value, safe='')}")

        for key in ("attachments", "files"):
            raw_value = message.get(key)
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                parsed = raw_value
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, str) and cls._looks_like_audio(item):
                    audio_refs.append(f"synology://file/{quote(item, safe='')}")
                elif isinstance(item, dict):
                    url = item.get("url") or item.get("file_url") or item.get("audio_url")
                    if not isinstance(url, str):
                        continue
                    content_type = (
                        item.get("content_type")
                        or item.get("mime_type")
                        or ""
                    ).lower()
                    name = (
                        item.get("name")
                        or item.get("filename")
                        or ""
                    ).lower()
                    if content_type.startswith("audio/") or cls._looks_like_audio(url) or name.endswith(cls._AUDIO_SUFFIXES):
                        audio_refs.append(f"synology://file/{quote(url, safe='')}")

        deduped = []
        for audio_ref in audio_refs:
            if audio_ref not in deduped:
                deduped.append(audio_ref)
        return deduped or None

    @classmethod
    def _looks_like_image(cls, value: str) -> bool:
        if not value or not isinstance(value, str):
            return False
        lowered = value.lower()
        return lowered.startswith("http") and any(
            suffix in lowered for suffix in cls._IMAGE_SUFFIXES
        )

    @classmethod
    def _looks_like_audio(cls, value: str) -> bool:
        if not value or not isinstance(value, str):
            return False
        lowered = value.lower()
        return lowered.startswith("http") and any(
            suffix in lowered for suffix in cls._AUDIO_SUFFIXES
        )

    def download_synologychat_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 Synology Chat 音频文件并返回原始字节
        """
        if not file_ref or not file_ref.startswith("synology://file/"):
            return None
        if not self.get_config(source):
            return None
        file_url = unquote(file_ref.replace("synology://file/", "", 1))
        resp = RequestUtils(timeout=30).get_res(file_url)
        if resp and resp.content:
            return resp.content
        return None

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
