"""
Telegram 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

import re

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelPlatformCapabilities,
)
from app.schemas.message import MessageAttachmentRead


class TelegramPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "telegram"
    channel = "Telegram"
    attachment_schemes = ("tg",)
    interaction = True
    text_confirmation = True
    binding_fields = ("telegram_userid",)
    edit_fields = ("TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_ID")
    public_fields = frozenset({"API_URL"})
    write_only = frozenset()
    default_targets = (("TELEGRAM_CHAT_ID", "conversation"),)
    bound_targets = (("telegram_userid", "user", True),)
    target_patterns = {"user": "[1-9][0-9]*", "conversation": "-?[1-9][0-9]*"}
    needs_target_review = False
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user", "group", "private"), target_kinds=("bound", "user", "conversation"), thread_id=True
    )
    user_as_chat = True
    topic_key = "TELEGRAM_TOPIC_ID"
    topic_pattern = r"[1-9][0-9]*"

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        第三方 Bot API 不声明宿主可确认的免审批身份事实。
        """
        trusted = connection.provider_config.get("API_URL") in (
            None,
            "",
            "https://api.telegram.org",
            "https://api.telegram.org/",
        )
        return self.capabilities_model.model_copy(
            update={"rule_kinds": self.capabilities_model.rule_kinds if trusted else ()}
        )

    def credential_key(self, connection: ChannelConnection) -> str | None:
        """
        只有官方 Bot API 连接可以通过官方身份诊断原地轮换。
        """
        return "TELEGRAM_TOKEN" if self.capabilities(connection).rule_kinds else None

    def probe_identity(self, connection: ChannelConnection) -> str | None:
        """
        调用官方 GetMe 确认机器人账号，所有路径都关闭诊断客户端。
        """
        from app.modules.telegram.botapi import TelegramApiError, TelegramBotApi, TelegramNetworkError
        from app.runtime.settings import get_runtime_setting

        token = connection.provider_config.get("TELEGRAM_TOKEN")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            return None
        client = TelegramBotApi(token, proxy=get_runtime_setting("PROXY"))
        try:
            info = client.get_me()
            bot_id = info.get("id")
            return str(bot_id) if info.get("is_bot") is True and type(bot_id) is int and bot_id > 0 else None
        except TelegramApiError, TelegramNetworkError, TypeError, ValueError:
            return None
        finally:
            client.close()

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        prefixes = {
            "image": ("tg://file_id/",),
            "audio": ("tg://voice_file_id/", "tg://audio_file_id/"),
            "file": ("tg://file_id/", "tg://document_file_id/"),
        }
        for prefix in prefixes.get(kind, ()):
            if reference.startswith(prefix):
                image = kind == "image"
                return MessageAttachmentRead(
                    method="download_telegram_file_to_base64" if image else "download_telegram_file_bytes",
                    parameters={"file_id": reference.removeprefix(prefix)},
                    encoding="base64_jpeg" if image else "bytes",
                    audio_filename="input.mp3" if prefix == "tg://audio_file_id/" else "input.ogg",
                    exact_filename=True,
                )
        return None
