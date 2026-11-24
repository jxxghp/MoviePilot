"""
Discord 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

import re
from typing import Any

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelPlatformCapabilities,
    InteractionPolicy,
)
from app.schemas.common import JsonData
from app.schemas.message import MessageAttachmentRead


class DiscordPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "discord"
    channel = "Discord"
    attachment_schemes = ("discord",)
    private_forbidden_prefixes = ("group:", "gid#")
    private_forbidden_suffixes = ("@chatroom",)
    interaction = True
    binding_fields = ("discord_userid",)
    edit_fields = ()
    public_fields = frozenset()
    write_only = frozenset()
    default_targets = (("DISCORD_CHANNEL_ID", "conversation"),)
    bound_targets = (("discord_userid", "user", True),)
    target_patterns = {"user": "[1-9][0-9]*", "conversation": "[1-9][0-9]*"}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user", "group", "private"),
        target_kinds=("bound", "user", "conversation"),
        rule_labels={"group": "channelSettings.ruleKind.channel"},
        target_labels={"conversation": "channelSettings.deliveryRules.channel"},
        interaction_form="discord-command",
    )
    conversation_as_chat = True
    literal_address_prefixes = ("#",)
    obsolete_fields = frozenset({"DISCORD_GUILD_ID"})

    def normalize_interaction(self, value: dict[str, Any]) -> dict[str, Any]:
        """
        读取既有 Discord 命令服务器字段，移入本模块的独立选项。
        """
        result = dict(value)
        legacy = result.pop("command_guild_id", None)
        options = dict(result.get("platform_options") or {})
        if legacy is not None and "command_guild_id" not in options:
            options["command_guild_id"] = legacy
        if legacy is not None or "platform_options" in result:
            result["platform_options"] = options
        return result

    def validate_interaction(self, options: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        命令注册作用域只接受单个 Discord 服务器数字 ID。
        """
        if options.keys() - {"command_guild_id"}:
            raise ValueError("Discord 不支持所提交的交互字段")
        raw = options.get("command_guild_id")
        if raw is not None and not isinstance(raw, str):
            raise ValueError("请输入有效的 Discord 服务器 ID")
        value = (raw or "").strip()
        if value and not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("请输入有效的 Discord 服务器 ID")
        return {"command_guild_id": value or None}

    def import_options(self, connection: ChannelConnection, policy: InteractionPolicy) -> InteractionPolicy:
        """
        将正式版本的命令服务器配置移交给 Discord 交互表单。
        """
        value = connection.provider_config.get("DISCORD_GUILD_ID")
        if (
            isinstance(value, (str, int))
            and re.fullmatch(r"[1-9][0-9]*", str(value))
            and not policy.platform_options.get("command_guild_id")
        ):
            return policy.model_copy(update={"platform_options": {"command_guild_id": str(value)}})
        return policy

    def runtime_config(self, connection: ChannelConnection, policy: InteractionPolicy | None) -> dict[str, JsonData]:
        """
        将命令服务器选项编码回 Discord 运行客户端需要的配置键。
        """
        config = dict(connection.provider_config)
        if policy and policy.platform_options.get("command_guild_id"):
            config["DISCORD_COMMAND_GUILD_ID"] = policy.platform_options["command_guild_id"]
        return config

    def export_interaction(self, policy: InteractionPolicy) -> dict[str, Any]:
        """
        公开读取保留旧 flat 字段；持久化只使用平台选项。
        """
        return policy.model_dump() | {"command_guild_id": policy.platform_options.get("command_guild_id")}

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind != "image" and reference.startswith("discord://file/"):
            return MessageAttachmentRead(method="download_discord_file_bytes", parameters={"file_ref": reference})
        return None
