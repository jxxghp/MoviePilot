"""
VoceChat 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.domain.authorization import ChannelActor
from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelPlatformCapabilities,
)
from app.schemas.message import MessageAttachmentRead


class VoceChatPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "vocechat"
    channel = "VoceChat"
    attachment_schemes = ("vocechat",)
    interaction = True
    binding_fields = ("vocechat_userid",)
    edit_fields = ("VOCECHAT_CHANNEL_ID",)
    public_fields = frozenset({"VOCECHAT_HOST"})
    write_only = frozenset()
    default_targets = (("VOCECHAT_CHANNEL_ID", "conversation"),)
    bound_targets = (("vocechat_userid", "user", True),)
    target_patterns = {"user": "[1-9][0-9]*", "conversation": "[1-9][0-9]*"}
    needs_target_review = False
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=(),
        target_kinds=("bound", "user", "conversation"),
        target_labels={"conversation": "channelSettings.deliveryRules.channel"},
        inbound_token=True,
    )
    conversation_prefix = "GID#"
    token_key = "VOCECHAT_INBOUND_TOKEN"

    def actor(self, value: str) -> ChannelActor:
        """
        VoceChat 数字 UID 与 UID# 等价，GID# 始终是群组。
        """
        text = value.strip()
        if text.upper().startswith("GID#"):
            return ChannelActor(text, "group")
        numeric = text[4:] if text.startswith("UID#") else text
        if numeric.isascii() and numeric.isdecimal():
            return ChannelActor("UID#" + numeric, aliases=(numeric,))
        return ChannelActor(text)

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        仅专用回调令牌认证后的事件能提供用户、群组和私聊规则事实。
        """
        kinds = ("user", "group", "private") if connection.provider_config.get("VOCECHAT_INBOUND_TOKEN") else ()
        return self.capabilities_model.model_copy(update={"rule_kinds": kinds})

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if reference.startswith("vocechat://file/"):
            if kind == "image":
                return MessageAttachmentRead(
                    method="download_vocechat_image_to_data_url",
                    parameters={"image_ref": reference},
                    encoding="data_url",
                )
            return MessageAttachmentRead(method="download_vocechat_file_bytes", parameters={"file_ref": reference})
        return None
