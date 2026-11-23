"""
企业微信通知模块的实例路由、回调解析与媒体下载入口。
"""

import json
import re
import time
import xml.dom.minidom
from collections.abc import Mapping
from functools import partial
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import quote

from app.adapters.external.wechat import WXBizMsgCrypt
from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.application.messaging.delivery import (
    NotificationParts,
    legacy_attachment_layout_active,
    notification_preflight_active,
)
from app.domain.admission import AdmissionProof
from app.domain.context import Context, MediaInfo
from app.foundation.dom import DomUtils
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, NotificationChannel


def _resolve_wechat_admin_ids(config: Optional[dict[str, object]]) -> set[str]:
    """
    解析企业微信管理员及机器人模式下的主用户 ID。
    """
    config_keys = ["WECHAT_ADMINS"]
    if (config or {}).get("WECHAT_MODE", "app") == "bot":
        config_keys.append("WECHAT_BOT_CHAT_ID")
    return resolve_config_principal_ids(config, *config_keys)


register_channel_admin_resolver(NotificationChannel.Wechat, _resolve_wechat_admin_ids)


class WechatModule(_MessageChannelModuleBase[WeChat | WeChatBot]):

    """
    按实例模式路由企业微信应用消息和智能机器人消息。
    """
    # 管理员配置键与渠道解析器保持一致
    _admin_config_key = "WECHAT_ADMINS"
    _notification_user_target_keys = ("wechat_userid",)
    _notification_admin_default_config_keys = ("WECHAT_BOT_CHAT_ID",)
    # 命令注册事件源标识固定为 WeChat（get_name 为“企业微信”）
    _command_origin = "WeChat"

    def init_module(self) -> None:
        """
        注册企业微信客户端工厂并加载各通知实例。
        """
        super().init_service(service_name=WeChat.__name__.lower(),
                             service_type=self._create_client)
        self._channel = NotificationChannel.Wechat

    @staticmethod
    def get_name() -> str:
        """
        返回通知配置中显示的渠道名称。

        :return: 企业微信
        """
        return "企业微信"

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
        声明模块处理企业微信渠道。

        :return: 企业微信渠道枚举
        """
        return NotificationChannel.Wechat

    @staticmethod
    def get_priority() -> int:
        """
        返回相同接口下的模块调度优先级。

        :return: 固定为 1；数值越小越先调用
        """
        return 1

    def stop(self) -> bool:
        """
        停止全部实例并等待客户端释放长连接。

        :return: 所有实例均成功停止时返回 True，否则返回 False
        """
        return self._stop_service_instances()

    @staticmethod
    def _is_bot_mode(config: Optional[Mapping[str, object]]) -> bool:
        """
        区分 Markdown 智能机器人与原有企业微信应用消息协议。
        """
        return (config or {}).get("WECHAT_MODE", "app") == "bot"

    @classmethod
    def _create_client(cls, conf: Any) -> WeChat | WeChatBot:
        """
        按实例模式创建企业微信应用或智能机器人客户端。
        """
        if cls._is_bot_mode(conf.config):
            return WeChatBot(**{
                **(conf.config or {}), "name": conf.name,
                "interaction_policy": getattr(conf, "interaction_policy", None),
            })
        return WeChat(name=conf.name, **conf.config)

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        return None

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[IncomingMessage]:
        """
        解析企业微信回调并检查管理员命令权限。

        应用模式支持文本、图片、语音、文件和菜单点击；机器人模式只接受私聊。
        解析错误记录日志后忽略该消息。

        :param source: 通知实例名称
        :param body: 应用模式的加密 XML，或机器人模式的 JSON 回调
        :param form: 保留的表单参数，当前不使用
        :param args: 应用模式所需的签名、时间戳和随机数查询参数
        :return: 统一消息；配置缺失、校验失败、内容不支持或无权限时返回 None
        """
        try:
            client_config = self.get_config(source)
            if not client_config:
                return None
            if self._is_bot_mode(client_config.config):
                return self._parse_bot_message(source=source, body=body, client_config=client_config)
            client = cast(WeChat, self.get_instance(client_config.name))
            sVerifyMsgSig = args.get("msg_signature")
            sVerifyTimeStamp = args.get("timestamp")
            sVerifyNonce = args.get("nonce")
            if not sVerifyMsgSig or not sVerifyTimeStamp or not sVerifyNonce:
                logger.debug(f"微信请求参数错误：{args}")
                return None
            config = client_config.config or {}
            wxcpt = WXBizMsgCrypt(sToken=config.get('WECHAT_TOKEN'),
                                  sEncodingAESKey=config.get('WECHAT_ENCODING_AESKEY'),
                                  sReceiveId=config.get('WECHAT_CORPID'))
            if not body:
                logger.debug("微信请求数据为空")
                return None
            logger.debug(f"收到微信请求：{body}")
            ret, sMsg = wxcpt.DecryptMsg(sPostData=body,
                                         sMsgSignature=sVerifyMsgSig,
                                         sTimeStamp=sVerifyTimeStamp,
                                         sNonce=sVerifyNonce)
            if ret != 0:
                logger.error(f"解密微信消息失败 DecryptMsg ret = {ret}")
                return None
            # DecryptMsg 的成功分支只返回解密后的 bytes，失败分支已在上方返回。
            dom_tree = xml.dom.minidom.parseString(cast(bytes, sMsg).decode('UTF-8'))
            # parseString 成功即有根元素；空文档会在解析阶段抛错。
            root_node = cast(xml.dom.minidom.Element, dom_tree.documentElement)
            msg_type = DomUtils.tag_value(root_node, "MsgType")
            # 菜单事件只处理 click，忽略进入应用等其他事件
            event = DomUtils.tag_value(root_node, "Event")
            user_id = DomUtils.tag_value(root_node, "FromUserName")
            # 缺少消息类型或发送者时无法路由消息
            if not msg_type or not user_id:
                logger.warn("解析不到消息类型和用户ID")
                return None
            content = None
            images = None
            audio_refs = None
            files = None
            if msg_type == "event" and event == "click":
                # 企业微信菜单最终会转成命令文本，需与斜杠命令使用一致的管理员校验。
                if self._should_reject_admin_command(client_config.config, user_id):
                    client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                    return None
                # 根据EventKey执行命令
                content = DomUtils.tag_value(root_node, "EventKey")
                logger.info(f"收到来自 {client_config.name} 的微信事件：userid={user_id}, event={content}")
            elif msg_type == "text":
                content = DomUtils.tag_value(root_node, "Content", default="")
                logger.info(f"收到来自 {client_config.name} 的微信消息：userid={user_id}, text={content}")
            elif msg_type == "image":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                pic_url = DomUtils.tag_value(root_node, "PicUrl")
                if media_id:
                    images = [IncomingMessage.MessageImage(ref=f"wxwork://media_id/{media_id}")]
                elif pic_url:
                    images = [IncomingMessage.MessageImage(ref=pic_url)]
                logger.info(
                    f"收到来自 {client_config.name} 的微信图片消息：userid={user_id}, images={len(images) if images else 0}"
                )
            elif msg_type == "voice":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                recognition = DomUtils.tag_value(root_node, "Recognition", default="")
                content = (recognition or "").strip()
                if media_id:
                    audio_refs = [f"wxwork://voice_media_id/{media_id}"]
                logger.info(
                    f"收到来自 {client_config.name} 的微信语音消息：userid={user_id}, "
                    f"text={content}, audios={len(audio_refs) if audio_refs else 0}"
                )
            elif msg_type == "file":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                file_name = DomUtils.tag_value(root_node, "FileName")
                if media_id:
                    files = [
                        IncomingMessage.MessageAttachment(
                            ref=f"wxwork://file_media_id/{media_id}",
                            name=file_name,
                        )
                    ]
                logger.info(
                    f"收到来自 {client_config.name} 的微信文件消息：userid={user_id}, files={len(files) if files else 0}"
                )
            else:
                return None

            if content and content.startswith("/") and self._should_reject_admin_command(
                    client_config.config, user_id
            ):
                client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                return None

            if content or images or audio_refs or files:
                incoming = IncomingMessage(channel=NotificationChannel.Wechat, source=client_config.name,
                                       conversation_type="private",
                                       userid=user_id, username=user_id,
                                       is_channel_admin=matches_channel_admin(
                                           NotificationChannel.Wechat,
                                           client_config.config,
                                           user_id,
                                        source=client_config.name), text=content or "",
                                       images=images, audio_refs=audio_refs, files=files)
                self._attach_verified_sender(incoming, client_config, root_node, user_id)
                return incoming
        except Exception as err:
            logger.error(f"微信消息处理发生错误：{str(err)}")
        return None

    @staticmethod
    def _attach_verified_sender(
        incoming: IncomingMessage, config: NotificationConf, root: xml.dom.minidom.Element, user_id: str,
    ) -> None:
        """
        仅在应用验签解密完成后附加账号接入证明。
        """
        policy = getattr(config, "interaction_policy", None)
        app_id = str((config.config or {}).get("WECHAT_APP_ID") or "")
        event_id = DomUtils.tag_value(root, "MsgId")
        if (
            not policy or not policy.enabled or not app_id
            or app_id != DomUtils.tag_value(root, "AgentID") or not event_id
        ):
            return
        try:
            received_at = int(DomUtils.tag_value(root, "CreateTime") or "0")
        except ValueError:
            return
        if abs(time.time() - received_at) <= 60:
            incoming.message_id = event_id
            incoming._admission_proof = AdmissionProof(
                config.id, policy.revision, user_id, None, received_at + 60, private=True,
            )

    def _parse_bot_message(
        self, source: str, body: Any, client_config: Any
    ) -> Optional[IncomingMessage]:
        """
        提取机器人私聊内容并检查管理员命令权限。

        :param source: 保留的来源参数，实际来源使用 client_config.name
        :param body: JSON 字节、文本或已解析的字典
        :param client_config: 当前实例配置，提供来源名称和管理员策略
        :return: 含附件和管理员标记的统一消息；群聊、空内容或未授权命令返回 None
        """
        try:
            if isinstance(body, bytes):
                msg_json = json.loads(body)
            elif isinstance(body, dict):
                msg_json = body
            else:
                msg_json = json.loads(body)
            while isinstance(msg_json, str):
                msg_json = json.loads(msg_json)
        except Exception as err:
            logger.debug(f"解析企业微信智能机器人消息失败：{err}")
            return None

        if not isinstance(msg_json, dict):
            return None

        payload_body = msg_json.get("body") or {}
        sender = ((payload_body.get("from") or {}).get("userid") or "").strip()
        if not sender:
            return None
        if payload_body.get("chattype") == "group":
            return None

        text = WeChatBot._extract_text_from_body(payload_body)
        images = WeChatBot._extract_images_from_body(payload_body)
        audio_refs = ["wxbot://voice"] if payload_body.get("msgtype") == "voice" else None
        files = None
        if payload_body.get("msgtype") == "file":
            file_payload = payload_body.get("file") or {}
            download_url = file_payload.get("download_url")
            if download_url:
                files = [
                    IncomingMessage.MessageAttachment(
                        ref=f"wxbot://file/{quote(download_url, safe='')}",
                        name=file_payload.get("name") or file_payload.get("filename"),
                        mime_type=file_payload.get("content_type")
                        or file_payload.get("mime_type"),
                        size=file_payload.get("size"),
                    )
                ]
        if text:
            text = re.sub(r"@\S+", "", text).strip()

        if text and text.startswith("/") and self._should_reject_admin_command(
                client_config.config, sender
        ):
            client = self.get_instance(client_config.name)
            if client:
                client.send_msg(title="只有管理员才有权限执行此命令", userid=sender)
            return None

        if not text and not images and not audio_refs and not files:
            return None

        logger.info(
            f"收到来自 {client_config.name} 的企业微信智能机器人消息："
            f"userid={sender}, text={text}, images={len(images) if images else 0}"
        )
        return IncomingMessage(
            channel=NotificationChannel.Wechat,
            source=client_config.name,
            userid=sender,
            conversation_type="private" if payload_body.get("chattype") == "single" else "unknown",
            username=sender,
            is_channel_admin=matches_channel_admin(
                NotificationChannel.Wechat,
                client_config.config,
                sender,
             source=client_config.name),
            text=text or "",
            images=images,
            audio_refs=audio_refs,
            files=files,
        )

    def _notification_target(self, message: Message, conf: NotificationConf) -> Optional[str]:
        """
        正常回复智能机器人群聊时保留明确的群地址，私聊入口已经清除了会话信息。
        """
        if self._is_bot_mode(conf.config) and not message.private_delivery and str(message.original_chat_id or "").startswith("group:"):
            return message.original_chat_id
        return self.get_notification_userid(message, "wechat_userid")

    def post_message(self, message: Message, **kwargs: object) -> Optional[bool]:
        """
        向符合通知规则的实例发送文本、图文、原生图片、文件或语音。

        :param message: 通知内容及接收对象；图片与文件分别保留，语音发送失败时回退到文本或图文
        :param kwargs: 兼容模块分发的附加参数，当前不使用
        :return: 强制私聊实际发送并确认后返回 True；普通通知或平台不匹配返回 None
        """
        message = self.prepare_message(message)
        for conf, message in self.iter_message_configs(message):
            userid = self._notification_target(message, conf)
            client = self.get_message_client(conf.name, message)
            if client is None:
                self.record_delivery(message, conf, None, target=userid, status="retryable_failure", code="client_unavailable")
                continue
            if isinstance(client, (WeChat, WeChatBot)):
                self._post_tracked_notification(client, conf, message, userid)
            else:
                if notification_preflight_active():
                    continue
                result = self._send_notification(client, conf, message, userid)
                self.record_delivery(message, conf, result, target=userid)
            if message.private_delivery and message.attachment_part_final:
                return True
        return None

    def _post_tracked_notification(
        self, client: WeChat | WeChatBot, conf: NotificationConf, message: Message, userid: Optional[str],
    ) -> None:
        """
        收集企业微信的逐目标和逐分段确认。

        :param client: 发送实例对应的企业微信客户端
        :param conf: 连接配置，提供不随显示名称变化的身份
        :param message: 当前派发的通知
        :param userid: 已选定的发送目标
        """
        parts = NotificationParts(NotificationChannel.Wechat, conf.id, conf.identity_namespace, part_offset=message.attachment_part_offset)
        text = message.channel_text(NotificationChannel.Wechat, conf.config) or message.text
        send = client.send_msg if isinstance(client, WeChatBot) else client.send_tracked_message

        image_options = {"native_image": True} if (
            isinstance(client, WeChatBot) and message.native_image and not legacy_attachment_layout_active()
        ) else {}

        def send_text() -> bool | None:
            """
            使用相同投递约束和原目标发送正文或图文。
            """
            return send(
                title=message.title, text=text, image=message.image, userid=userid, link=message.link,
                receipt=partial(parts.record, message), part_sender=parts.send, plan=parts.plan, **image_options,
            )

        result: bool | None
        if message.file_path and isinstance(client, WeChatBot) and legacy_attachment_layout_active():
            parts.record(message, userid, 0, "permanent_failure", "unsupported_attachment")
            result = False
        elif message.file_path and isinstance(client, WeChatBot):
            result = client.send_file(
                file_path=message.file_path, file_name=message.file_name, title=message.title,
                text=text, userid=userid, receipt=partial(parts.record, message), part_sender=parts.send, plan=parts.plan,
            )
        elif isinstance(client, WeChat) and message.file_path:
            result = client.send_tracked_media(
                message.file_path, message.file_name, media_type="file", title=message.title, text=text,
                userid=userid, receipt=partial(parts.record, message), part_sender=parts.send, plan=parts.plan,
            )
        elif isinstance(client, WeChat) and message.native_image and message.image:
            result = client.send_tracked_media(
                message.image, None, media_type="image", title=message.title, text=text,
                userid=userid, receipt=partial(parts.record, message), part_sender=parts.send, plan=parts.plan,
            )
        elif isinstance(client, WeChat) and message.voice_path:
            result = client.send_tracked_voice(
                message.voice_path, userid, receipt=partial(parts.record, message), part_sender=parts.send, plan=parts.plan,
                fallback=send_text if (message.title or text or message.image) else None,
            )
        else:
            result = send_text()
        self.confirm_private_delivery(message, result)

    @staticmethod
    def _send_notification(
        client: WeChat | WeChatBot, config: NotificationConf, message: Message, userid: Optional[str],
    ) -> Optional[bool]:
        """
        投递企业微信最终选择的内容，文本回退使用同一个目标。

        :param client: 当前配置对应的应用或机器人客户端
        :param config: 用于选择渲染格式的实例配置
        :param message: 带正文和可选语音的通知
        :param userid: 该次投递的明确用户标识
        :return: 企业微信应用接口或长连接发回的确认结果
        """
        if message.voice_path and hasattr(client, "send_voice"):
            sent = client.send_voice(voice_path=message.voice_path, userid=userid)
            if sent:
                return sent
        text = message.channel_text(NotificationChannel.Wechat, config.config) or message.text
        if message.file_path:
            return client.send_file(
                file_path=message.file_path, file_name=message.file_name, title=message.title, text=text, userid=userid,
            )
        if message.native_image:
            return client.send_msg(title=message.title, text=text, image=message.image, userid=userid,
                                   link=message.link, native_image=True)
        return client.send_msg(title=message.title, text=text, image=message.image, userid=userid, link=message.link)

    def download_wechat_image_to_data_url(self, image_ref: str, source: str) -> Optional[str]:
        """
        通过来源实例下载企业微信图片。

        :param image_ref: 应用媒体引用 wxwork://media_id/ 或机器人图片引用 wxbot://image/
        :param source: 通知实例名称
        :return: 含图片 MIME 类型的 Base64 数据地址；引用不支持、实例缺失或下载失败返回 None
        """
        if not image_ref:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        if image_ref.startswith("wxwork://media_id/") and hasattr(client, "download_media_to_data_url"):
            media_id = image_ref.replace("wxwork://media_id/", "", 1)
            result = client.download_media_to_data_url(media_id)
            return result if isinstance(result, str) else None
        if image_ref.startswith("wxbot://image/") and hasattr(client, "download_image_to_data_url"):
            result = client.download_image_to_data_url(image_ref)
            return result if isinstance(result, str) else None
        return None

    def download_wechat_media_bytes(self, media_ref: str, source: str) -> Optional[bytes]:
        """
        通过应用实例下载语音或文件的原始内容。

        :param media_ref: wxwork://voice_media_id/ 或 wxwork://file_media_id/ 引用
        :param source: 通知实例名称
        :return: 媒体字节；引用不支持、实例缺失或下载失败返回 None
        """
        if not media_ref:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client or not hasattr(client, "download_media_bytes"):
            return None
        if media_ref.startswith("wxwork://voice_media_id/"):
            media_id = media_ref.replace("wxwork://voice_media_id/", "", 1)
            return client.download_media_bytes(media_id)
        if media_ref.startswith("wxwork://file_media_id/"):
            media_id = media_ref.replace("wxwork://file_media_id/", "", 1)
            return client.download_media_bytes(media_id)
        return None

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        先发送通知标题，再发送媒体候选列表。

        :param message: 通知路由条件、接收对象和标题
        :param medias: 按原顺序展示的媒体候选项
        """
        message = self.prepare_message(message)
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_message_client(conf.name, message)
            if client:
                userid = self._notification_target(message, conf)
                self.confirm_private_delivery(message, client.send_msg(title=message.title, userid=userid, link=message.link))
                self.confirm_private_delivery(message, client.send_medias_msg(medias=medias, userid=userid))

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        向符合通知规则的实例发送种子候选列表。

        :param message: 通知路由条件、接收对象、标题和链接
        :param torrents: 含种子及媒体信息的候选上下文
        """
        message = self.prepare_message(message)
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_message_client(conf.name, message)
            if client:
                userid = self._notification_target(message, conf)
                self.confirm_private_delivery(message, client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=userid, link=message.link))

    def _commands_enabled(self, config: Optional[dict[str, Any]]) -> bool:
        """
        菜单注册前置条件：智能机器人模式无传统菜单，缺少解密参数时无法调用菜单 API。
        """
        if self._is_bot_mode(config):
            logger.debug("智能机器人模式，跳过传统菜单初始化")
            return False
        config = config or {}
        if not config.get("WECHAT_ENCODING_AESKEY") or not config.get("WECHAT_TOKEN"):
            logger.debug("缺少消息解密参数，跳过菜单初始化")
            return False
        return True

    def _delete_commands(self, client: Any) -> None:
        """
        企业微信使用自定义菜单 API 清理命令。
        """
        client.delete_menus()

    def _apply_commands(self, client: WeChat, commands: Dict[str, dict[str, object]]) -> None:
        """
        企业微信使用自定义菜单 API 注册命令。
        """
        client.create_menus(commands)
