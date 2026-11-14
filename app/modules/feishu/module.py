"""
飞书通知模块的实例管理、投递路由和统一消息接口。
"""

from typing import Any, List, Optional, Tuple, Union

from app.application.messaging.channel.admin import (
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.feishu.feishu import Feishu
from app.schemas.message import IncomingMessage, Message, MessageResponse
from app.schemas.types import ModuleType, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.Feishu,
    lambda config: resolve_config_principal_ids(
        config, "FEISHU_ADMINS", "FEISHU_OPEN_ID"
    ),
)


class FeishuModule(_MessageChannelModuleBase[Feishu]):
    """
    按通知实例路由飞书消息、附件、卡片更新和处理状态。
    """
    _notification_user_target_keys = (
        "feishu_openid",
        "feishu_userid",
        "feishu_chat_id",
    )
    _notification_admin_default_config_keys = ("FEISHU_OPEN_ID", "FEISHU_CHAT_ID")

    def init_module(self) -> None:
        """
        加载飞书通知实例并绑定渠道类型。
        """
        super().init_service(service_name=Feishu.__name__.lower(), service_type=Feishu)
        self._channel = NotificationChannel.Feishu

    @staticmethod
    def get_name() -> str:
        """
        返回配置界面使用的渠道名称。

        :return: 飞书
        """
        return "飞书"

    @staticmethod
    def get_type() -> ModuleType:
        """
        声明模块属于通知服务。

        :return: 通知模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        声明模块使用飞书通知渠道。

        :return: 飞书渠道枚举
        """
        return NotificationChannel.Feishu

    @staticmethod
    def get_priority() -> int:
        """
        返回同一接口的模块调度优先级。

        :return: 固定为 2；数值越小越先调用
        """
        return 2

    def _commands_enabled(self, config: Optional[dict[str, Any]]) -> bool:
        """
        飞书机器人无斜杠命令概念，开放平台也不提供命令注册/删除 API，。
        跳过命令注册，避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> bool:
        """
        停止全部飞书实例并等待长连接退出。

        :return: 全部实例均停止成功时返回 True，否则返回 False
        """
        return self._stop_service_instances()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        通知模块通过系统通知配置控制实例化，这里不额外设置环境开关。
        """
        return None

    @staticmethod
    def _resolve_message_target(
            message: Message,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        从通知内容解析用户、会话和接收标识类型。

        要求私聊且有用户时只按用户寻址；否则优先回到原会话，再使用通知用户及渠道目标。

        :param message: 包含私聊约束、原会话和渠道目标的消息
        :return: 用户标识、会话标识和接收标识类型的三元组；缺少的项为 None
        """
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
        """
        交给来源实例解析统一入口报文。

        :param source: 通知实例名称
        :param body: JSON 文本、字节或已解析的报文
        :param form: 兼容其他渠道的表单参数，当前不使用
        :param args: 兼容其他渠道的查询参数，当前不使用
        :return: 统一消息；实例缺失或报文被拒绝时返回 None
        """
        client_config = self.get_config(source)
        if not client_config:
            return None
        client: Optional[Feishu] = self.get_instance(client_config.name)
        if not client:
            return None
        return client.parse_message(body)

    def post_message(self, message: Message, **kwargs: Any) -> None:
        """
        向符合通知规则的实例发送卡片、附件或语音。

        :param message: 通知内容和目标；同时有封面与文件时分别发送卡片和附件
        :param kwargs: 兼容模块分发的附加参数，当前不使用
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Optional[Feishu] = self.get_instance(conf.name)
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
        """
        向符合通知规则的实例发送媒体候选卡片。

        :param message: 标题、按钮和接收目标等通知信息
        :param medias: 媒体候选列表，客户端最多展示前 10 项
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Optional[Feishu] = self.get_instance(conf.name)
            if client:
                client.send_medias_message(
                    message=message,
                    medias=medias,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        向符合通知规则的实例发送种子候选卡片。

        :param message: 标题、按钮和接收目标等通知信息
        :param torrents: 种子候选列表，客户端最多展示前 10 项
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Optional[Feishu] = self.get_instance(conf.name)
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
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[bool]:
        """
        通过指定实例更新普通卡片或流式卡片。

        :param channel: 目标通知渠道；非飞书时不处理
        :param source: 原消息所属通知实例
        :param message_id: 原消息标识
        :param chat_id: 流式图片补发使用的会话标识
        :param text: 新的正文
        :param title: 普通卡片的新标题
        :param buttons: 新按钮布局；提供按钮时使用普通卡片更新
        :param metadata: 原消息流式状态，客户端会原地更新序号和重试信息
        :return: 更新成功返回 True，失败或实例缺失返回 False；渠道不匹配返回 None
        """
        if channel != self._channel:
            return None
        for conf in self.get_configs().values():
            if source != conf.name:
                continue
            client: Optional[Feishu] = self.get_instance(conf.name)
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
        """
        发送消息并返回首个成功实例的消息引用。

        封面与附件组合只以卡片发送结果决定成功，附件发送结果不影响返回值。

        :param message: 待发送通知；plain 模式通过纯文本保留敏感字符，封面与文件分别发送
        :return: 成功主消息的引用与可选流式状态；没有成功实例时返回 None
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid, chat_id, receive_id_type = self._resolve_message_target(message)
            client: Optional[Feishu] = self.get_instance(conf.name)
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
        """
        下载飞书图片引用并编码为数据地址。

        优先下载原消息资源，失败后尝试机器人上传的图片接口。

        :param image_ref: feishu://image/ 引用，可携带原消息标识
        :param source: 用于下载的通知实例名称
        :return: Base64 图片数据地址；引用无效、实例缺失或下载失败返回 None
        """
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
        """
        下载飞书文件或音频引用的原始内容。

        带原消息时依次尝试音频资源和文件资源，随后回退到机器人文件下载接口。

        :param file_ref: feishu://file/ 引用，可携带 om_ 开头的原消息标识
        :param source: 用于下载的通知实例名称
        :return: 资源字节；引用无效、实例缺失或下载失败返回 None
        """
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
        """
        通过指定实例给消息添加表情回应。

        :param message_id: 目标消息标识
        :param emoji_type: 飞书表情名称
        :param source: 通知实例名称
        :return: 回应标识；实例缺失或接口失败时返回 None
        """
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
        """
        通过指定实例删除消息表情回应。

        :param message_id: 目标消息标识
        :param reaction_id: 待删除回应标识
        :param source: 通知实例名称
        :return: 删除成功返回 True，实例缺失或接口失败返回 False
        """
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
    ) -> Optional[dict[str, Any]]:
        """
        为有文本的普通消息添加“正在处理”表情。

        :param channel: 来源渠道；非飞书时忽略
        :param source: 来源通知实例
        :param userid: 发送者标识，保存在状态中供后续使用
        :param message_id: 需要标记的原消息标识
        :param chat_id: 原会话标识，保存在状态中
        :param text: 用于排除空内容和 CALLBACK: 按钮回调的正文
        :return: 含回应标识的处理状态；不适用或添加失败返回 None
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
            status: Optional[dict[str, Any]] = None,
    ) -> Optional[bool]:
        """
        根据起始处理状态删除“正在处理”表情。

        :param channel: 来源渠道；非飞书时忽略
        :param source: 来源通知实例
        :param userid: 兼容统一接口的用户标识，当前不使用
        :param message_id: 状态中无消息标识时使用的原消息标识
        :param chat_id: 兼容统一接口的会话标识，当前不使用
        :param status: 起始阶段返回的处理状态，必须包含回应标识
        :return: 删除成功返回 True；缺少状态或删除失败返回 False；渠道不匹配返回 None
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

    def finalize_message(self, response: MessageResponse) -> bool:
        """
        为流式卡片提交下一个序号并关闭流式展示。

        :param response: 包含飞书来源及 feishu_streaming 状态的发送结果
        :return: 成功关闭返回 True；渠道、状态或实例不适用及接口失败返回 False
        """
        if response.channel != self._channel or not isinstance(response.metadata, dict):
            return False
        raw_stream_meta = response.metadata.get("feishu_streaming")
        if not isinstance(raw_stream_meta, dict):
            return False
        stream_meta: dict[str, Any] = raw_stream_meta
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
