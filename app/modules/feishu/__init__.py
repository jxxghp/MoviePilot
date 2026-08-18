from typing import Any, List, Optional, Tuple, Union

from app.domain.context import Context, MediaInfo
from app.runtime.channels import register_channel_admin_resolver, resolve_config_principal_ids
from app.runtime.log import logger
from app.modules._base import _MessageChannelModuleBase
from app.modules.feishu.feishu import Feishu
from app.schemas.message import IncomingMessage
from app.schemas.notification import NotificationChannel
from app.schemas.message import MessageResponse
from app.schemas.message import Message


register_channel_admin_resolver(
    NotificationChannel.Feishu,
    lambda config: resolve_config_principal_ids(
        config, "FEISHU_ADMINS", "FEISHU_OPEN_ID"
    ),
)


class FeishuModule(_MessageChannelModuleBase[Feishu]):
    def init_module(self) -> None:
        super().init_service(service_name=Feishu.__name__.lower(), service_type=Feishu)
        self._channel = NotificationChannel.Feishu

    @staticmethod
    def get_name() -> str:
        return "飞书"

    @staticmethod
    def get_subtype() -> NotificationChannel:
        return NotificationChannel.Feishu

    @staticmethod
    def get_priority() -> int:
        return 2

    def _commands_enabled(self, config: Optional[dict]) -> bool:
        """
        飞书机器人无斜杠命令概念，lark_oapi Client 也不提供命令注册/删除 API，
        跳过命令注册，避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止飞书模块实例失败：{err}")

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """通知模块通过系统通知配置控制实例化，这里不额外设置环境开关。"""
        return None

    @staticmethod
    def _resolve_message_target(
            message: Message,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """解析发送目标：交互式回复优先回到原会话（群聊@回复必须回原群），其次 open_id，最后回退 user_id 或 chat_id。"""
        userid = str(message.userid).strip() if message.userid else None
        chat_id = None
        receive_id_type = "open_id" if userid else None

        # 私聊投递只能按用户身份寻址，原会话 ID 可能属于群聊。
        if message.private_delivery and userid:
            return userid, None, None

        # 回复类消息携带原会话 ID 时，必须发回原会话，
        # 否则群聊 @ 机器人的回复会错误地发送到机器人与用户的私聊窗口。
        original_chat_id = str(message.original_chat_id or "").strip() or None
        if original_chat_id:
            return None, original_chat_id, "chat_id"

        targets = message.targets or {}
        if not userid and targets:
            open_id = str(targets.get("feishu_openid") or "").strip() or None
            user_id = str(targets.get("feishu_userid") or "").strip() or None
            chat_id = str(targets.get("feishu_chat_id") or "").strip() or None
            if open_id:
                userid = open_id
                receive_id_type = "open_id"
            elif user_id:
                userid = user_id
                receive_id_type = "user_id"

        return userid, chat_id, receive_id_type

    def message_parser(
            self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Feishu = self.get_instance(client_config.name)
        if not client:
            return None
        return client.parse_message(body)

    def post_message(self, message: Message, **kwargs) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Feishu = self.get_instance(conf.name)
            if client:
                if message.image and message.file_path:
                    # 普通文件无法嵌入卡片，先发送图文卡片，再单独发送附件，避免图片被 file_path 分支吞掉。
                    client.send_notification(
                        message=message.model_copy(update={"file_path": None, "file_name": None}),
                        userid=userid,
                        chat_id=chat_id,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )
                    client.send_file(
                        file_path=message.file_path,
                        userid=userid,
                        chat_id=chat_id,
                        file_name=message.file_name,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )
                elif message.file_path:
                    client.send_file(
                        file_path=message.file_path,
                        userid=userid,
                        chat_id=chat_id,
                        title=message.title,
                        text=message.text,
                        file_name=message.file_name,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )
                elif message.voice_path:
                    client.send_voice(
                        voice_path=message.voice_path,
                        userid=userid,
                        chat_id=chat_id,
                        caption=message.voice_caption,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )
                else:
                    client.send_notification(
                        message=message,
                        userid=userid,
                        chat_id=chat_id,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Feishu = self.get_instance(conf.name)
            if client:
                client.send_medias_message(
                    message=message,
                    medias=medias,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Feishu = self.get_instance(conf.name)
            if client:
                client.send_torrents_message(
                    message=message,
                    torrents=torrents,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )

    def edit_message(
            self,
            channel: NotificationChannel,
            source: str,
            message_id: Union[str, int],
            chat_id: Union[str, int],
            text: str,
            title: Optional[str] = None,
            buttons: Optional[List[List[dict]]] = None,
            metadata: Optional[dict] = None,
    ) -> Optional[bool]:
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Feishu = self.get_instance(conf.name)
            if client and client.edit_message(
                    message_id=str(message_id),
                    title=title,
                    text=text,
                    buttons=buttons,
                    metadata=metadata,
                    chat_id=str(chat_id) if chat_id else None,
            ):
                return True
        return False

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Feishu = self.get_instance(conf.name)
            if not client:
                continue
            if message.image and message.file_path:
                result = client.send_notification(
                    message=message.model_copy(update={"file_path": None, "file_name": None}),
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=str(message.original_message_id) if message.original_message_id else None,
                )
                if result and result.get("success"):
                    client.send_file(
                        file_path=message.file_path,
                        userid=userid,
                        chat_id=chat_id,
                        file_name=message.file_name,
                        receive_id_type=receive_id_type,
                        original_message_id=str(message.original_message_id) if message.original_message_id else None,
                    )
            elif message.file_path:
                result = client.send_file(
                    file_path=message.file_path,
                    userid=userid,
                    chat_id=chat_id,
                    title=message.title,
                    text=message.text,
                    file_name=message.file_name,
                    receive_id_type=receive_id_type,
                    original_message_id=str(message.original_message_id) if message.original_message_id else None,
                )
            elif message.voice_path:
                result = client.send_voice(
                    voice_path=message.voice_path,
                    userid=userid,
                    chat_id=chat_id,
                    caption=message.voice_caption,
                    receive_id_type=receive_id_type,
                    original_message_id=str(message.original_message_id) if message.original_message_id else None,
                )
            elif str(message.parse_mode or "").strip().lower() == "plain":
                # 受保护结果必须绕过 Markdown 卡片，避免密钥字符被解释或改写。
                plain_text = "\n".join(
                    part
                    for part in (message.title, message.text, message.link)
                    if part
                )
                result = client.send_text(
                    text=plain_text,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=str(message.original_message_id) if message.original_message_id else None,
                )
            else:
                result = client.send_notification(
                    message=message,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=str(message.original_message_id) if message.original_message_id else None,
                )
            if result and result.get("success"):
                return MessageResponse(
                    message_id=result.get("message_id"),
                    chat_id=result.get("chat_id"),
                    channel=NotificationChannel.Feishu,
                    source=conf.name,
                    metadata=result.get("metadata"),
                    success=True,
                )
        return None

    def download_feishu_image_to_data_url(self, image_ref: str, source: str) -> Optional[str]:
        if not image_ref or not image_ref.startswith("feishu://image/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        resource_path = image_ref.replace("feishu://image/", "", 1)
        message_id = None
        image_key = resource_path
        if "/" in resource_path:
            message_id, image_key = resource_path.split("/", 1)
            message_id = message_id.strip() or None
            image_key = image_key.strip()
        downloaded = None
        if message_id:
            downloaded = client.download_message_resource_bytes(
                message_id=message_id,
                file_key=image_key,
                resource_type="image",
            )
        if not downloaded:
            downloaded = client.download_image_bytes(image_key)
        if not downloaded:
            return None
        content, _, content_type = downloaded
        mime_type = content_type or "image/jpeg"
        import base64

        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"

    def download_feishu_file_bytes(self, file_ref: str, source: str) -> Optional[bytes]:
        if not file_ref or not file_ref.startswith("feishu://file/"):
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        parts = [
            part.strip()
            for part in file_ref.replace("feishu://file/", "", 1).split("/")
            if part.strip()
        ]
        file_key = ""
        downloaded = None
        if len(parts) >= 2 and parts[0].startswith("om_"):
            message_id, file_key = parts[0], parts[1]
            downloaded = client.download_message_resource_bytes(
                message_id=message_id,
                file_key=file_key,
                resource_type="audio",
            )
            if not downloaded:
                downloaded = client.download_message_resource_bytes(
                    message_id=message_id,
                    file_key=file_key,
                    resource_type="file",
                )
        else:
            file_key = parts[0] if parts else ""
        if not file_key:
            return None
        if not downloaded:
            downloaded = client.download_file_bytes(file_key)
        if not downloaded:
            return None
        content, _, _ = downloaded
        return content

    def add_feishu_message_reaction(
            self,
            message_id: str,
            emoji_type: str,
            source: str,
    ) -> Optional[str]:
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        return client.add_message_reaction(message_id=message_id, emoji_type=emoji_type)

    def delete_feishu_message_reaction(
            self,
            message_id: str,
            reaction_id: str,
            source: str,
    ) -> Optional[bool]:
        client_config = self.get_config(source)
        if not client_config:
            return False
        client = self.get_instance(client_config.name)
        if not client:
            return False
        return client.delete_message_reaction(message_id=message_id, reaction_id=reaction_id)

    def mark_message_processing_started(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            text: Optional[str] = None,
    ) -> Optional[dict]:
        """
        使用飞书消息表情标记“正在处理”。
        """
        if channel != self._channel:
            return None
        if not message_id or not text or str(text).startswith("CALLBACK:"):
            return None
        reaction_id = self.add_feishu_message_reaction(
            message_id=str(message_id),
            emoji_type=Feishu.PROCESSING_REACTION_EMOJI,
            source=source,
        )
        if not reaction_id:
            return None
        return {
            "channel": channel.value,
            "source": source,
            "userid": userid,
            "message_id": str(message_id),
            "chat_id": str(chat_id) if chat_id else None,
            "metadata": {
                "kind": "reaction",
                "reaction_id": str(reaction_id),
                "emoji_type": Feishu.PROCESSING_REACTION_EMOJI,
            },
        }

    def mark_message_processing_finished(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            status: Optional[dict] = None,
    ) -> Optional[bool]:
        """
        删除飞书“正在处理”表情。
        """
        if channel != self._channel:
            return None
        metadata = (status or {}).get("metadata") or {}
        target_message_id = (status or {}).get("message_id") or message_id
        reaction_id = metadata.get("reaction_id")
        if not target_message_id or not reaction_id:
            return False
        return self.delete_feishu_message_reaction(
            message_id=str(target_message_id),
            reaction_id=str(reaction_id),
            source=source,
        )

    def finalize_message(self, response: MessageResponse) -> Optional[bool]:
        # 非本渠道返回 None 表示让出：单播按「首个非空答案」仲裁，返回 False 会被当成
        # 已认领而短路，把真正该收尾这条消息的渠道挡在后面
        if response.channel != self._channel:
            return None
        if not isinstance(response.metadata, dict):
            return False
        stream_meta = response.metadata.get("feishu_streaming") or {}
        card_id = str(stream_meta.get("card_id") or "").strip()
        if not card_id:
            return False
        client_config = self.get_config(response.source)
        if not client_config:
            return False
        client = self.get_instance(client_config.name)
        if not client:
            return False
        sequence = int(stream_meta.get("sequence") or 0) + 1
        return client.close_streaming_card(card_id=card_id, sequence=sequence)
