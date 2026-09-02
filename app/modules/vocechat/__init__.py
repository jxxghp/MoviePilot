import json
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, unquote

from app.application.messaging.agent import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.vocechat.vocechat import VoceChat
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.notification import NotificationChannel
from app.schemas.types import ModuleType

register_channel_admin_resolver(
    NotificationChannel.VoceChat,
    lambda config: resolve_config_principal_ids(config, "VOCECHAT_ADMINS"),
)


class VoceChatModule(_MessageChannelModuleBase[VoceChat]):
    # 管理员配置键，与渠道 resolver 保持一致
    _admin_config_key = "VOCECHAT_ADMINS"
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
        super().init_service(service_name=VoceChat.__name__.lower(),
                             service_type=VoceChat)
        self._channel = NotificationChannel.VoceChat

    @staticmethod
    def get_name() -> str:
        return "VoceChat"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        获取模块子类型
        """
        return NotificationChannel.VoceChat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def _send_admin_denied(
            client: Optional[VoceChat], userid: Optional[Union[str, int]]
    ) -> None:
        """
        向 VoceChat 非管理员用户发送命令拒绝提示。
        """
        if client and userid:
            client.send_msg(title="只有管理员才有权限执行此命令", userid=str(userid))

    @staticmethod
    def _mention_ids(detail: dict) -> set[str]:
        """读取 VoceChat 文本消息 properties.mentions 中的用户 ID。"""
        properties = detail.get("properties") or {}
        mentions = properties.get("mentions") if isinstance(properties, dict) else None
        if not isinstance(mentions, list):
            return set()
        return {
            str(mention).strip()
            for mention in mentions
            if str(mention).strip().isdigit()
        }

    @classmethod
    def _is_mentioned_group_message(cls, detail: dict, config: dict) -> bool:
        """群消息仅在明确 @ 时触发；配置机器人 ID 后只接受对机器人的 @。"""
        mentions = cls._mention_ids(detail)
        if not mentions:
            return False
        bot_id = str(
            config.get("VOCECHAT_BOT_ID")
            or config.get("bot_id")
            or ""
        ).strip()
        if not bot_id.isdigit() or int(bot_id) <= 0:
            return False
        return bot_id in mentions

    @staticmethod
    def _mention_only_enabled(config: dict) -> bool:
        """读取群聊仅 @ 回复开关，旧配置默认保持安全的开启状态。"""
        value = config.get("VOCECHAT_MENTION_ONLY")
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[IncomingMessage]:
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
            """
            {
              "created_at": 1672048481664, //消息创建的时间戳
              "detail": {
                "content": "hello this is my message to you", //消息内容
                "content_type": "text/plain", //消息类型，text/plain：纯文本消息，text/markdown：markdown消息，vocechat/file：文件类消息
                "expires_in": null, //消息过期时长，如果有大于0数字，说明该消息是个限时消息
                "properties": null, //一些有关消息的元数据，比如at信息，文件消息的具体类型信息，如果是个图片消息，还会有一些宽高，图片名称等元信息
                "type": "normal" //消息类型，normal代表是新消息
              },
              "from_uid": 7910, //来自于谁
              "mid": 2978, //消息ID
              "target": { "gid": 2 } //发送给谁，gid代表是发送给频道，uid代表是发送给个人，此时的数据结构举例：{"uid":1}
            }
            """
            # 获取服务配置
            client_config = self.get_config(source)
            if not client_config:
                return None
            client: VoceChat = self.get_instance(client_config.name)
            # 报文体
            msg_body = json.loads(body)
            # 类型
            msg_type = msg_body.get("detail", {}).get("type")
            if msg_type not in ("normal", "reply"):
                # 非新消息/回复
                return None
            logger.debug(f"收到VoceChat请求：{msg_body}")
            detail = msg_body.get("detail", {}) or {}
            content_type = detail.get("content_type") or ""
            content = detail.get("content")
            images = self._extract_images(detail)
            audio_refs = self._extract_audio_refs(detail)
            files = self._extract_files(detail)
            text = None
            if content_type in ("text/plain", "text/markdown") and isinstance(content, str):
                text = content
            # 用户ID
            target = msg_body.get("target") or {}
            if not isinstance(target, dict):
                logger.warning(
                    f"忽略目标结构无效的VoceChat消息：target_type={type(target).__name__}"
                )
                return None
            gid = target.get("gid")
            target_uid = target.get("uid")
            from_uid = msg_body.get("from_uid")
            if from_uid is None:
                return None
            actor_userid = f"UID#{from_uid}"
            channel_id = (
                client_config.config.get("VOCECHAT_CHANNEL_ID")
                or client_config.config.get("channel_id")
            )
            logger.debug(
                "VoceChat消息路由：target_keys=%s, gid=%s, target_uid=%s, "
                "configured_channel_id=%s, mention_ids=%s",
                sorted(target.keys()), gid, target_uid, channel_id,
                sorted(self._mention_ids(detail)),
            )
            if gid is not None:
                if str(gid) != str(channel_id):
                    logger.debug(
                        f"忽略非监听频道的VoceChat群消息：gid={gid}, "
                        f"configured_channel_id={channel_id}, from_uid={from_uid}"
                    )
                    return None
                # 来自监听频道的消息
                if (
                        self._mention_only_enabled(client_config.config)
                        and not self._is_mentioned_group_message(
                            detail,
                            client_config.config,
                        )
                ):
                    logger.debug(
                        f"忽略未@机器人的VoceChat群消息：gid={gid}, from_uid={from_uid}"
                    )
                    return None
                userid = f"GID#{gid}"
            elif target_uid is not None:
                # 来自个人的消息
                userid = f"UID#{from_uid}"
            else:
                logger.debug(
                    f"忽略目标类型不明的VoceChat消息：target_keys={sorted(target.keys())}, "
                    f"from_uid={from_uid}"
                )
                return None

            # 处理消息内容
            if (text or images or audio_refs or files) and userid:
                if text and text.startswith("/") and self._should_reject_admin_command(
                        client_config.config, from_uid, actor_userid
                ):
                    self._send_admin_denied(client, userid)
                    return None
                logger.info(
                    f"收到来自 {client_config.name} 的VoceChat消息："
                    f"userid={userid}, text={text}, images={len(images) if images else 0}, "
                    f"audios={len(audio_refs) if audio_refs else 0}, files={len(files) if files else 0}"
                )
                return IncomingMessage(channel=NotificationChannel.VoceChat, source=client_config.name,
                                      userid=userid, username=userid,
                                      is_channel_admin=matches_channel_admin(
                                          NotificationChannel.VoceChat, client_config.config,
                                          from_uid, actor_userid,
                                      ), text=text or "",
                                      images=images, audio_refs=audio_refs, files=files)
        except Exception as err:
            logger.error(f"VoceChat消息处理发生错误：{str(err)}")
        return None

    @classmethod
    def _extract_images(
        cls, detail: dict
    ) -> Optional[List[IncomingMessage.MessageImage]]:
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        direct_url = (
            properties.get("url")
            or properties.get("download_url")
            or properties.get("file_url")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        ).lower()

        is_image = mime_type.startswith("image/") or file_name.endswith(cls._IMAGE_SUFFIXES)
        if not is_image:
            return None
        if isinstance(direct_url, str) and direct_url.startswith("http"):
            return [
                IncomingMessage.MessageImage(
                    ref=direct_url,
                    name=properties.get("name") or properties.get("filename"),
                    mime_type=mime_type or None,
                    size=properties.get("size"),
                )
            ]
        if isinstance(file_path, str) and file_path:
            return [
                IncomingMessage.MessageImage(
                    ref=f"vocechat://file/{quote(file_path, safe='')}",
                    name=properties.get("name") or properties.get("filename"),
                    mime_type=mime_type or None,
                    size=properties.get("size"),
                )
            ]
        return None

    @classmethod
    def _extract_audio_refs(cls, detail: dict) -> Optional[List[str]]:
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        ).lower()

        is_audio = mime_type.startswith("audio/") or file_name.endswith(cls._AUDIO_SUFFIXES)
        if not is_audio:
            return None
        if isinstance(file_path, str) and file_path:
            return [f"vocechat://file/{quote(file_path, safe='')}"]
        return None

    @classmethod
    def _extract_files(
        cls, detail: dict
    ) -> Optional[List[IncomingMessage.MessageAttachment]]:
        content_type = detail.get("content_type") or ""
        if content_type != "vocechat/file":
            return None
        properties = detail.get("properties") or {}
        mime_type = (
            properties.get("content_type")
            or properties.get("mime_type")
            or properties.get("contentType")
            or ""
        ).lower()
        file_path = (
            properties.get("path")
            or properties.get("file_path")
            or properties.get("storage_path")
            or detail.get("content")
        )
        file_name = (
            properties.get("name")
            or properties.get("filename")
            or (str(file_path).rsplit("/", 1)[-1] if file_path else "")
        )
        lowered_name = str(file_name).lower()
        is_image = mime_type.startswith("image/") or lowered_name.endswith(
            cls._IMAGE_SUFFIXES
        )
        is_audio = mime_type.startswith("audio/") or lowered_name.endswith(
            cls._AUDIO_SUFFIXES
        )
        if is_image or is_audio or not isinstance(file_path, str) or not file_path:
            return None
        return [
            IncomingMessage.MessageAttachment(
                ref=f"vocechat://file/{quote(file_path, safe='')}",
                name=file_name,
                mime_type=properties.get("content_type")
                or properties.get("mime_type")
                or properties.get("contentType"),
                size=properties.get("size"),
            )
        ]

    def post_message(self, message: Message, **kwargs) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not message.userid and targets:
                userid = targets.get('vocechat_userid')
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, text=message.text,
                                image=message.image, userid=userid, link=message.link)

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息内容
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, userid=message.userid)
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=message.userid, link=message.link)

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message: 消息内容
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get('vocechat_userid')
                if not userid:
                    logger.warn("用户没有指定 VoceChat用户ID，消息无法发送")
                    return
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict]):
        pass

    def download_vocechat_image_to_data_url(self, image_ref: str, source: str) -> Optional[str]:
        """
        下载 VoceChat 图片并转换为 data URL
        """
        if not image_ref or not image_ref.startswith("vocechat://file/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: VoceChat = self.get_instance(client_config.name)
        if not client:
            return None
        file_path = unquote(image_ref.replace("vocechat://file/", "", 1))
        return client.download_file_to_data_url(file_path)

    def download_vocechat_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        """
        下载 VoceChat 文件并返回原始字节
        """
        if not file_ref or not file_ref.startswith("vocechat://file/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: VoceChat = self.get_instance(client_config.name)
        if not client:
            return None
        file_path = unquote(file_ref.replace("vocechat://file/", "", 1))
        file_data = client.download_file(file_path)
        if file_data:
            content, _ = file_data
            return content
        return None
