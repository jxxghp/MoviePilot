"""
Feishu 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelPlatformCapabilities,
)
from app.schemas.message import MessageAttachmentRead


class FeishuPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "feishu"
    channel = "飞书"
    attachment_schemes = ("feishu",)
    private_forbidden_prefixes = ("group:", "gid#")
    private_forbidden_suffixes = ("@chatroom",)
    private_pattern = '(?!oc_).*'
    interaction = True
    text_confirmation = True
    binding_fields = ("feishu_userid", "feishu_openid")
    edit_fields = ("FEISHU_OPEN_ID", "FEISHU_CHAT_ID", "FEISHU_APP_SECRET")
    public_fields = frozenset({"FEISHU_APP_ID"})
    write_only = frozenset({"FEISHU_APP_SECRET"})
    default_targets = (("FEISHU_OPEN_ID", "user"), ("FEISHU_CHAT_ID", "conversation"))
    bound_targets = (
        ("feishu_openid", "user", True),
        ("feishu_userid", "user", False),
        ("feishu_chat_id", "conversation", True),
    )
    target_patterns = {"user": "ou_.*", "conversation": "oc_.*"}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user", "group", "private"), target_kinds=("bound", "user", "conversation")
    )
    conversation_as_chat = True
    conversation_as_user = False
    obsolete_fields = frozenset({"FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY"})

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind == "image" and reference.startswith("feishu://image/"):
            return MessageAttachmentRead(
                method="download_feishu_image_to_data_url", parameters={"image_ref": reference}, encoding="data_url"
            )
        if kind != "image" and reference.startswith("feishu://file/"):
            return MessageAttachmentRead(
                method="download_feishu_file_bytes", parameters={"file_ref": reference}, audio_filename="input.opus"
            )
        return None
