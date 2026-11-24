"""
QQ 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.domain.authorization import ChannelActor
from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelPlatformCapabilities,
)
from app.schemas.message import MessageAttachmentRead


class QQPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "qqbot"
    channel = "QQ"
    attachment_schemes = ("qq",)
    private_forbidden_prefixes = ("group:", "gid#")
    private_forbidden_suffixes = ("@chatroom",)
    interaction = True
    binding_fields = ("qq_userid", "qq_openid")
    edit_fields = ("QQ_OPENID", "QQ_GROUP_OPENID", "QQ_APP_SECRET")
    public_fields = frozenset({"QQ_APP_ID"})
    write_only = frozenset({"QQ_APP_SECRET"})
    default_targets = (("QQ_GROUP_OPENID", "conversation"), ("QQ_OPENID", "user"))
    bound_targets = (
        ("qq_userid", "user", True),
        ("qq_openid", "user", True),
        ("qq_group_openid", "conversation", True),
        ("qq_group", "conversation", True),
    )
    target_patterns = {"user": "[^:\\s]+", "conversation": "[^:\\s]+"}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("group", "user"), target_kinds=("bound", "user", "conversation")
    )
    conversation_prefix = "group:"

    def actor(self, value: str) -> ChannelActor:
        """
        QQ 群组 OpenID 的兼容前缀是会话地址，不能作为个人授权主体。
        """
        text = value.strip()
        return ChannelActor(text, "group" if text.lower().startswith("group:") else "person")

    def conversation(self, address: str) -> str:
        """
        将认证群事件中的兼容会话前缀还原为群组 OpenID。
        """
        return address.removeprefix("group:")

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind != "image" and reference.startswith("qq://file/"):
            return MessageAttachmentRead(method="download_qq_file_bytes", parameters={"file_ref": reference})
        return None
