"""
SynologyChat 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelPlatformCapabilities,
    NotificationRule,
)
from app.schemas.message import Message, MessageAttachmentRead


class SynologyChatPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "synologychat"
    channel = "SynologyChat"
    attachment_schemes = ("synology",)
    interaction = True
    binding_fields = ("synologychat_userid",)
    edit_fields = ("SYNOLOGYCHAT_WEBHOOK",)
    public_fields = frozenset()
    write_only = frozenset({"SYNOLOGYCHAT_WEBHOOK"})
    default_targets = ()
    bound_targets = (("synologychat_userid", "user", True),)
    target_patterns = {"user": "[1-9][0-9]*"}
    needs_target_review = False
    capabilities_model = ChannelPlatformCapabilities(rule_kinds=(), target_kinds=("bound", "user", "all"))
    all_address = "@all"

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        专用平台令牌存在时才提供可验证的用户规则。
        """
        return self.capabilities_model.model_copy(
            update={"rule_kinds": ("user",) if connection.provider_config.get("SYNOLOGYCHAT_TOKEN") else ()}
        )

    def address_message(self, message: Message, rule: NotificationRule) -> Message:
        """
        平台全体发送仍使用模块原有的空地址广播，不能编码为个人地址。
        """
        if rule.kind == "all":
            return message.model_copy(update={"targets": None})
        return super().address_message(message, rule)

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind != "image" and reference.startswith("synology://file/"):
            return MessageAttachmentRead(method="download_synologychat_file_bytes", parameters={"file_ref": reference})
        return None
