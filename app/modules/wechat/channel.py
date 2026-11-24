"""
Wechat 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

import re
from collections.abc import Mapping
from urllib.parse import unquote

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelMode,
    ChannelPlatformCapabilities,
    NotificationRule,
    NotificationTargetKind,
    RuleKind,
)
from app.schemas.common import JsonData
from app.schemas.message import MessageAttachmentRead


class WechatPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "wechat"
    channel = "微信"
    attachment_schemes = ("wxwork", "wxbot")
    interaction = True
    binding_fields = ("wechat_userid",)
    edit_fields = ()
    public_fields = frozenset({"WECHAT_BOT_ID", "WECHAT_PROXY", "WECHAT_APP_ID", "WECHAT_CORPID", "WECHAT_BOT_WS_URL"})
    write_only = frozenset()
    default_targets = (("WECHAT_BOT_CHAT_ID", "user"),)
    bound_targets = (("wechat_userid", "user", True),)
    target_patterns = {}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=(),
        target_kinds=("bound", "user", "all"),
        credential_label="channelSettings.newAppSecret",
        credential_action="channelSettings.rotateAppSecret",
    )
    conversation_prefix = "group:"
    all_address = "@all"

    def mode(self, config: Mapping[str, JsonData]) -> ChannelMode:
        """
        兼容正式版本企业微信应用与智能机器人两种接入模式。
        """
        return "bot" if config.get("WECHAT_MODE") == "bot" else "app"

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        企业微信只有具备对应认证材料的模式才提供可信事件条件。
        """
        config = connection.provider_config
        kinds: tuple[RuleKind, ...] = ()
        if (
            connection.mode == "bot"
            and config.get("WECHAT_BOT_ID")
            and config.get("WECHAT_BOT_SECRET")
            and (
                not config.get("WECHAT_BOT_WS_URL") or config["WECHAT_BOT_WS_URL"] == "wss://openws.work.weixin.qq.com"
            )
        ):
            kinds = ("user", "group", "private")
        if connection.mode == "app" and all(
            config.get(key) for key in ("WECHAT_CORPID", "WECHAT_APP_ID", "WECHAT_ENCODING_AESKEY", "WECHAT_TOKEN")
        ):
            kinds = ("user", "private")
        targets = ("bound", "user", "conversation") if connection.mode == "bot" else ("bound", "user", "all")
        return self.capabilities_model.model_copy(update={"rule_kinds": kinds, "target_kinds": targets})

    def editable_fields(self, connection: ChannelConnection) -> tuple[str, ...]:
        """
        自建应用的回调认证可单独编辑；官方机器人只编辑当前身份的连接密钥。
        """
        if connection.mode == "app":
            return ("WECHAT_TOKEN", "WECHAT_ENCODING_AESKEY")
        if connection.provider_config.get("WECHAT_BOT_WS_URL") not in (None, "", "wss://openws.work.weixin.qq.com"):
            return ()
        return ("WECHAT_BOT_SECRET",)

    def patch_config(self, connection: ChannelConnection, patch: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        企业微信认证字段留空时保留原值，身份字段仍受允许列表保护。
        """
        if patch.keys() - set(self.editable_fields(connection)):
            raise ValueError("连接平台身份及受保护字段需通过凭据轮换或新建连接修改")
        return connection.provider_config | {key: value for key, value in patch.items() if value not in (None, "")}

    def form_values(self, connection: ChannelConnection) -> dict[str, JsonData]:
        """
        公开接入模式和身份标识，不回显回调或机器人密钥。
        """
        return {key: value for key, value in connection.provider_config.items() if key in self.public_fields} | {
            "WECHAT_MODE": connection.mode
        }

    def credential_key(self, connection: ChannelConnection) -> str | None:
        """
        只有能由官方接口确认身份的自建应用允许单独轮换 secret。
        """
        if connection.mode == "app" and connection.provider_config.get("WECHAT_PROXY") in (
            None,
            "",
            "https://qyapi.weixin.qq.com",
            "https://qyapi.weixin.qq.com/",
        ):
            return "WECHAT_APP_SECRET"
        return None

    def probe_identity(self, connection: ChannelConnection) -> str | None:
        """
        用企业微信应用身份诊断确认 corp 与 agent 的归属。
        """
        from app.modules.wechat.wechat import WeChat

        config = connection.provider_config
        app_id, corp_id, secret = (
            config.get("WECHAT_APP_ID"),
            config.get("WECHAT_CORPID"),
            config.get("WECHAT_APP_SECRET"),
        )
        if (
            connection.mode == "app"
            and isinstance(corp_id, str)
            and isinstance(secret, str)
            and type(app_id) in (str, int)
        ):
            return WeChat.probe_app_identity(corp_id, str(app_id), secret)
        return None

    def validate_target(self, connection: ChannelConnection, rule: NotificationRule) -> None:
        """
        指定目标必须是单个企业微信地址，全体广播只能使用独立目标类型。
        """
        if rule.target is not None and (
            rule.target.lower() == "@all" or not re.fullmatch(r"[A-Za-z0-9_@.-]+", rule.target)
        ):
            raise ValueError("请输入单个企业微信用户或群组 ID")
        super().validate_target(connection, rule)

    def legacy_addresses(
        self, connection: ChannelConnection
    ) -> tuple[tuple[NotificationTargetKind, str, str | None], ...]:
        """
        只导入机器人默认目标，区分原有用户与群组前缀。
        """
        if connection.mode != "bot":
            return ()
        value = connection.provider_config.get("WECHAT_BOT_CHAT_ID")
        address = str(value).strip() if type(value) in (str, int) else ""
        if not address:
            return ()
        kind: NotificationTargetKind = "conversation" if address.lower().startswith("group:") else "user"
        if address.lower().startswith(("group:", "user:")):
            address = address.split(":", 1)[1].strip()
        return ((kind, address, None),)

    def conversation(self, address: str) -> str:
        """
        将认证机器人群事件的兼容地址还原为企业微信群组 ID。
        """
        return address.removeprefix("group:")

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind == "image" and reference.startswith(("wxwork://media_id/", "wxbot://image/")):
            return MessageAttachmentRead(
                method="download_wechat_image_to_data_url", parameters={"image_ref": reference}, encoding="data_url"
            )
        if kind == "audio" and reference.startswith("wxwork://voice_media_id/"):
            return MessageAttachmentRead(
                method="download_wechat_media_bytes",
                parameters={"media_ref": reference},
                audio_filename="input.amr",
                exact_filename=True,
            )
        if kind == "file":
            if reference.startswith(("wxwork://media_id/", "wxwork://file_media_id/")):
                return MessageAttachmentRead(method="download_wechat_media_bytes", parameters={"media_ref": reference})
            if reference.startswith("wxbot://image/"):
                return MessageAttachmentRead(
                    method="download_wechat_image_to_data_url", parameters={"image_ref": reference}, encoding="data_url"
                )
            if reference.startswith("wxbot://file/"):
                return MessageAttachmentRead(url=unquote(reference.removeprefix("wxbot://file/")))
        return None
