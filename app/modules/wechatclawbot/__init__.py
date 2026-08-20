import json
from typing import Any, Dict, List, Optional, Tuple, Union

from app.runtime.cache import TTLCache
from app.domain.context import Context, MediaInfo
from app.runtime.channels import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.runtime.log import logger
from app.modules._base import _MessageChannelModuleBase
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.schemas.message import IncomingMessage
from app.schemas.message import Message
from app.schemas.types import NotificationChannel, NotificationAction


register_channel_admin_resolver(
    NotificationChannel.WechatClawBot,
    lambda config: resolve_config_principal_ids(
        config, "WECHATCLAWBOT_ADMINS", "WECHATCLAWBOT_DEFAULT_TARGET"
    ),
)


class WechatClawBotModule(_MessageChannelModuleBase[WechatClawBot]):
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
        super().init_service(
            service_name=WechatClawBot.__name__.lower(), service_type=WechatClawBot
        )
        self._channel = NotificationChannel.WechatClawBot

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "微信 ClawBot"

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """获取模块子类型。"""
        return NotificationChannel.WechatClawBot

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级。"""
        return 2

    def _commands_enabled(self, config: Optional[dict]) -> bool:
        """
        微信爪爪机器人客户端未提供命令注册/删除 API，跳过命令注册，
        避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                client.stop()
            except Exception as err:
                logger.error(f"停止微信 ClawBot 模块实例失败：{err}")

    def _test_connection(self, client) -> Tuple[bool, str]:
        """微信 ClawBot 的连接探测返回 (状态, 信息)。"""
        return client.test_connection()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """初始化模块设置。"""
        pass

    def channel_manage(
            self,
            channel: NotificationChannel,
            action: NotificationAction,
            **params: Any,
    ) -> Optional[Dict[str, Any]]:
        """通知渠道通用管理入口，按渠道名路由，仅处理本渠道。

        动作语义与表单参数全部由模块自行解释：优先使用已保存配置实例，
        无匹配配置时可基于表单参数构造临时实例（未保存配置的扫码预览）。
        统一返回 {"success": bool, "message": ..., "data": ...} 结构。
        """
        # 路由标识归一化：兼容枚举名、枚举值与原始枚举对象
        if isinstance(channel, str) and channel not in (self.get_subtype().name, self.get_subtype().value):
            return None
        if not isinstance(channel, str) and channel != self.get_subtype():
            return None
        try:
            action = NotificationAction(action)
        except ValueError:
            return {"success": False, "message": f"不支持的渠道管理动作：{action}"}

        if action == NotificationAction.MIGRATE_CACHE:
            success, message = WechatClawBot.migrate_cached_state(
                old_name=params.get("old_name"),
                new_name=params.get("new_name"),
                cleanup_old=bool(params.get("cleanup_old")),
                overwrite=bool(params.get("overwrite")),
            )
            return {"success": success, "message": message}

        client, errmsg = self._resolve_client(params)
        if not client:
            return {"success": False, "message": errmsg}

        if action == NotificationAction.STATUS:
            data = client.get_status(
                refresh_remote=bool(params.get("refresh_remote", True)),
                auto_generate_qrcode=bool(params.get("auto_generate_qrcode", True)),
            )
            return {"success": bool(data.get("success")), "message": data.get("message"), "data": data}
        if action == NotificationAction.REFRESH_QRCODE:
            data = client.refresh_qrcode()
            return {"success": bool(data.get("success")), "message": data.get("message"), "data": data}
        if action == NotificationAction.LOGOUT:
            data = client.logout()
            return {"success": bool(data.get("success")), "message": data.get("message"), "data": data}
        if action == NotificationAction.TEST_CONNECTION:
            state, message = client.test_connection()
            return {"success": state, "message": message}
        return {"success": False, "message": f"不支持的渠道管理动作：{action.value}"}

    def _resolve_client(self, params: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
        """解析微信 ClawBot 客户端实例，返回 (客户端, 错误信息)。

        优先使用已加载的配置实例，均无配置时退回到基于表单参数的临时客户端，
        用于未保存配置的扫码状态预览。请求未带渠道名且本模块有多个已启用配置时，
        把无法确定目标的原因作为错误信息交给调用方展示。
        """
        source_name = str(params.get("source") or "").strip() or None
        fallback_name = str(params.get("fallback_source") or "").strip() or None

        candidate_names = []
        for candidate in (fallback_name, source_name):
            if candidate and candidate not in candidate_names:
                candidate_names.append(candidate)
        if candidate_names:
            for candidate in candidate_names:
                config = self.get_config(candidate)
                if not config:
                    continue
                client = self.get_instance(config.name)
                if client:
                    return client, None
        else:
            try:
                client = self.get_instance()
            except LookupError as err:
                return None, str(err)
            if client:
                return client, None

        temp_client = self._build_temp_client(params)
        if temp_client:
            return temp_client, None

        if source_name:
            return None, f"未找到名为 {source_name} 的微信 ClawBot 通知配置"
        return None, "微信 ClawBot 通知未启用或配置尚未保存，请先保存并启用当前渠道"

    def _build_temp_client(self, params: Dict[str, Any]) -> Optional[Any]:
        """基于表单参数创建临时客户端，用于未保存配置时的扫码状态预览。"""
        source_name = str(params.get("source") or params.get("fallback_source") or "").strip()
        if not source_name:
            return None
        return WechatClawBot(
            name=source_name,
            WECHATCLAWBOT_BASE_URL=params.get("WECHATCLAWBOT_BASE_URL"),
            WECHATCLAWBOT_DEFAULT_TARGET=params.get("WECHATCLAWBOT_DEFAULT_TARGET"),
            WECHATCLAWBOT_ADMINS=params.get("WECHATCLAWBOT_ADMINS"),
            WECHATCLAWBOT_POLL_TIMEOUT=params.get("WECHATCLAWBOT_POLL_TIMEOUT"),
            auto_start_polling=False,
        )

    @staticmethod
    def _load_json(body: Any) -> Optional[dict]:
        """将内容解析为 JSON 字典。"""
        if isinstance(body, dict):
            payload = body
        elif isinstance(body, bytes):
            payload = json.loads(body.decode("utf-8", errors="replace"))
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
    def _normalize_files(files: Any) -> Optional[List[IncomingMessage.MessageAttachment]]:
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
                IncomingMessage.MessageAttachment(
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
    ) -> Optional[IncomingMessage]:
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
        images = IncomingMessage.MessageImage.normalize_list(message.get("images"))
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
        callback_data = text[9:].strip() if text.startswith("CALLBACK:") else ""
        is_admin_command = text.startswith("/") or callback_data.startswith("/")
        is_channel_admin = matches_channel_admin(
            NotificationChannel.WechatClawBot,
            client_config.config,
            user_id,
        )
        if is_admin_command and admins and not is_channel_admin:
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
        return IncomingMessage(
            channel=NotificationChannel.WechatClawBot,
            source=client_config.name,
            userid=user_id,
            username=username,
            is_channel_admin=is_channel_admin,
            text=text,
            message_id=message_id,
            chat_id=str(message.get("chat_id") or "") or None,
            images=images,
            audio_refs=audio_refs,
            files=files,
        )

    def post_message(self, message: Message, **kwargs) -> None:
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

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """发送媒体选择列表。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: WechatClawBot = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(medias=medias, userid=message.userid)

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
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
