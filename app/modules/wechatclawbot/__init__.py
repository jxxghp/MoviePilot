"""
微信 ClawBot 通知模块的实例管理、消息去重和登录管理入口。
"""

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from app.application.messaging.channel.admin import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.runtime.cache import TTLCache
from app.runtime.log import logger
from app.schemas.message import IncomingMessage, Message
from app.schemas.types import ModuleType, NotificationAction, NotificationChannel

register_channel_admin_resolver(
    NotificationChannel.WechatClawBot,
    lambda config: resolve_config_principal_ids(
        config, "WECHATCLAWBOT_ADMINS", "WECHATCLAWBOT_DEFAULT_TARGET"
    ),
)


class WechatClawBotModule(_MessageChannelModuleBase[WechatClawBot]):
    """
    管理微信 ClawBot 实例、登录操作、入站去重和通知投递。
    """
    _notification_user_target_keys = ("wechatclawbot_userid",)
    _notification_admin_default_config_keys = ("WECHATCLAWBOT_DEFAULT_TARGET",)

    def __init__(self) -> None:
        """
        创建实例级入站消息去重缓存。

        按来源和消息标识缓存最多 8192 项，保留 7 天；没有消息标识的报文不参与去重。
        """
        super().__init__()
        # iLink 偶发会重复回放同一条 update，这里按 message_id 做渠道内幂等保护。
        self._recent_message_ids = TTLCache(
            region="wechatclawbot_message_dedup",
            maxsize=8192,
            ttl=7 * 24 * 60 * 60,
        )

    def init_module(self) -> None:
        """
        加载微信 ClawBot 通知实例并绑定渠道类型。
        """
        super().init_service(
            service_name=WechatClawBot.__name__.lower(), service_type=WechatClawBot
        )
        self._channel = NotificationChannel.WechatClawBot

    @staticmethod
    def get_name() -> str:
        """
        返回配置界面使用的渠道名称。

        :return: 微信 ClawBot
        """
        return "微信 ClawBot"

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
        声明模块处理微信 ClawBot 渠道。

        :return: 微信 ClawBot 渠道枚举
        """
        return NotificationChannel.WechatClawBot

    @staticmethod
    def get_priority() -> int:
        """
        返回同一接口的模块调度优先级。

        :return: 固定为 2；数值越小越先调用
        """
        return 2

    def _commands_enabled(self, config: Optional[dict[str, object]]) -> bool:
        """
        微信爪爪机器人客户端未提供命令注册/删除 API，跳过命令注册，。
        避免基类默认钩子调用不存在的 client.register_commands。
        """
        return False

    def stop(self) -> bool:
        """
        请求停止全部实例的消息轮询。

        :return: 所有实例的轮询线程均退出时返回 True，否则返回 False
        """
        return self._stop_service_instances()

    def _test_connection(self, client: WechatClawBot) -> Tuple[bool, str]:
        """
        微信 ClawBot 的连接探测返回 (状态, 信息)。
        """
        return client.test_connection()

    def _service_kwargs(self, conf: Any) -> Dict[str, Any]:
        """
        将稳定通知身份传递给 ClawBot 客户端。
        """
        return {"name": conf.name, "identity": conf.id, **conf.config}

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留设置初始化钩子，实例配置由通知服务管理。
        """
        return None

    def channel_manage(
            self,
            channel: NotificationChannel,
            action: NotificationAction,
            **params: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        执行本渠道的登录、状态查询、连接探测和缓存迁移操作。

        优先使用已保存实例，查不到时可创建不自动轮询的临时实例。
        缓存迁移读取 old_name、new_name、cleanup_old 和 overwrite；配置同步读取 previous 与 current。

        :param channel: 渠道枚举、枚举名或枚举值；其他渠道不处理
        :param action: NotificationAction 管理动作
        :param params: 动作参数；source 或 fallback_source 定位实例，也可提供未保存的表单配置
        :return: 含 success、message 及可选 data 的结果；渠道不匹配返回 None
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

        if action == NotificationAction.RECONCILE_CONFIG:
            return WechatClawBot.reconcile_cached_states(
                params.get("previous") or [], params.get("current") or []
            )

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
        """
        解析微信 ClawBot 客户端实例，返回 (客户端, 错误信息)。

        优先使用已加载的配置实例，均无配置时退回到基于表单参数的临时客户端，
        用于未保存配置的扫码状态预览。
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
                    config = next(
                        (item for item in self.get_configs().values() if item.id == candidate),
                        None,
                    )
                if not config:
                    continue
                client = self.get_instance(config.name)
                if client:
                    return client, None
        else:
            client = self.get_instance()
            if client:
                return client, None

        temp_client = self._build_temp_client(params)
        if temp_client:
            return temp_client, None

        if source_name:
            return None, f"未找到名为 {source_name} 的微信 ClawBot 通知配置"
        return None, "微信 ClawBot 通知未启用或配置尚未保存，请先保存并启用当前渠道"

    def _build_temp_client(self, params: Dict[str, Any]) -> Optional[Any]:
        """
        基于表单参数创建临时客户端，用于未保存配置时的扫码状态预览。
        """
        source_name = str(params.get("source") or params.get("fallback_source") or "").strip()
        fallback_name = str(params.get("fallback_source") or "").strip()
        if not source_name:
            return None
        return WechatClawBot(
            name=fallback_name or source_name,
            identity=source_name,
            WECHATCLAWBOT_BASE_URL=params.get("WECHATCLAWBOT_BASE_URL"),
            WECHATCLAWBOT_DEFAULT_TARGET=params.get("WECHATCLAWBOT_DEFAULT_TARGET"),
            WECHATCLAWBOT_ADMINS=params.get("WECHATCLAWBOT_ADMINS"),
            WECHATCLAWBOT_POLL_TIMEOUT=params.get("WECHATCLAWBOT_POLL_TIMEOUT"),
            auto_start_polling=False,
        )

    @staticmethod
    def _load_json(body: Any) -> Optional[Dict[str, Any]]:
        """
        将内容解析为 JSON 字典。
        """
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
        """
        标准化音频引用列表。
        """
        if not audio_refs:
            return None
        if not isinstance(audio_refs, list):
            audio_refs = [audio_refs]
        normalized = [str(item).strip() for item in audio_refs if str(item).strip()]
        return normalized or None

    @staticmethod
    def _normalize_files(files: Any) -> Optional[List[IncomingMessage.MessageAttachment]]:
        """
        标准化文件附件列表。
        """
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
        """
        检查来源内消息是否已出现，并立即记录首次出现的标识。

        记录发生在权限检查之前；此操作不声明并发检查与写入的原子性。

        :param source: 通知实例名称
        :param message_id: 入站消息标识；None 或空字符串不参与去重
        :return: 命中缓存返回 True，否则返回 False
        """
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
        """
        规范化 ClawBot 入站消息并执行去重与命令权限检查。

        :param source: 通知实例名称
        :param body: JSON 文本、字节或字典，兼容重复编码的 JSON 字符串
        :param form: 兼容其他渠道的表单参数，当前不使用
        :param args: 兼容其他渠道的查询参数，当前不使用
        :return: 含用户、附件和管理员标记的统一消息；无效、空内容、重复或未授权命令返回 None
        """
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
            # 渠道报文被解析为空消息时必须留下诊断信息，否则只能在消息入口看到
            # “未识别到有效消息”，无法定位是哪种入站内容被丢弃。
            logger.info(
                "微信 ClawBot 消息未识别到有效内容：source=%s, userid=%s, message_id=%s, body=%s",
                client_config.name,
                user_id,
                message_id,
                WechatClawBot._short_text(message, max_len=500),
            )
            return None
        if self._is_duplicate_message(client_config.name or "", message_id):
            logger.info(
                "忽略重复的微信 ClawBot 消息：source=%s, userid=%s, message_id=%s",
                client_config.name,
                user_id,
                message_id,
            )
            return None

        config = client_config.config or {}
        admins = [
            admin.strip()
            for admin in str(config.get("WECHATCLAWBOT_ADMINS") or "").split(",")
            if admin.strip()
        ]
        callback_data = text[9:].strip() if text.startswith("CALLBACK:") else ""
        is_admin_command = text.startswith("/") or callback_data.startswith("/")
        is_channel_admin = matches_channel_admin(
            NotificationChannel.WechatClawBot,
            config,
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

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        向符合通知规则的实例发送附件、语音文件或普通通知。

        :param message: 通知内容与接收目标；普通文件优先，其次把语音作为文件发送
        :param kwargs: 兼容模块分发的附加参数，当前不使用
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            userid = self.get_notification_userid(
                message, "wechatclawbot_userid"
            )
            client: Optional[WechatClawBot] = self.get_instance(conf.name)
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
        """
        通过来源实例下载 ClawBot 图片。

        :param image_ref: wxclaw://image/ 内部图片引用
        :param source: 通知实例名称
        :return: Base64 图片数据地址；引用无效、实例缺失或下载失败返回 None
        """
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
        """
        通过来源实例下载 ClawBot 语音或附件。

        :param media_ref: wxclaw://file/ 或 wxclaw://voice/ 内部引用
        :param source: 通知实例名称
        :return: 资源字节；引用无效、实例缺失或下载失败返回 None
        """
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
        """
        向符合通知规则的实例发送媒体候选列表。

        :param message: 通知路由条件和接收目标
        :param medias: 按输入顺序展示的媒体候选项
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Optional[WechatClawBot] = self.get_instance(conf.name)
            if client:
                client.send_medias_msg(
                    medias=medias,
                    userid=self.get_notification_userid(
                        message, "wechatclawbot_userid"
                    ),
                )

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        向符合通知规则的实例发送种子候选列表。

        :param message: 通知路由条件、标题、链接和接收目标
        :param torrents: 含种子和媒体信息的候选上下文
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: Optional[WechatClawBot] = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(
                    torrents=torrents,
                    userid=self.get_notification_userid(
                        message, "wechatclawbot_userid"
                    ),
                    title=message.title,
                    link=message.link,
                )
