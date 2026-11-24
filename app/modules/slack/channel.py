"""
Slack 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelPlatformCapabilities,
)
from app.schemas.message import Message, MessageAttachmentRead


class SlackPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "slack"
    channel = "Slack"
    attachment_schemes = ("slack",)
    private_forbidden_prefixes = ("group:", "gid#")
    private_forbidden_suffixes = ("@chatroom",)
    private_pattern = '[UW][A-Z0-9]+'
    interaction = True
    binding_fields = ("slack_userid",)
    edit_fields = ("SLACK_CHANNEL",)
    public_fields = frozenset()
    write_only = frozenset()
    default_targets = (("SLACK_CHANNEL", "conversation"),)
    bound_targets = (("slack_userid", "user", True),)
    target_patterns = {"user": "[UW][A-Z0-9]+", "conversation": "[CGD][A-Z0-9]+|#[^\\s]+"}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user", "group", "private"),
        target_kinds=("bound", "user", "conversation"),
        rule_labels={"group": "channelSettings.ruleKind.channel"},
        target_labels={"conversation": "channelSettings.deliveryRules.channel"},
    )
    conversation_as_chat = True
    literal_address_prefixes = ("#",)
    rotation_key = "SLACK_OAUTH_TOKEN"

    def probe_identity(self, connection: ChannelConnection) -> str | None:
        """
        使用 Slack 官方认证诊断确认工作区与机器人身份。
        """
        from app.modules.slack.slack import Slack

        token = connection.provider_config.get("SLACK_OAUTH_TOKEN")
        return Slack.probe_workspace_identity(token) if isinstance(token, str) else None

    def split_notification(self, parts: list[Message]) -> list[Message]:
        """
        Slack 原图上传与交互按钮独立发送，顺序及内容与既有冻结计划保持一致。
        """
        first = parts[0]
        if first.native_image and first.image and first.buttons:
            caption = first.model_copy(update={"image": None, "native_image": False, "link": None})
            image = first.model_copy(update={"title": None, "text": None, "buttons": None, "link": None})
            return [caption, image, *parts[1:]]
        return parts

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if kind == "image" or (kind == "file" and reference.startswith("http")):
            return MessageAttachmentRead(
                method="download_slack_file_to_data_url", parameters={"file_url": reference}, encoding="data_url"
            )
        if reference.startswith("slack://file/"):
            return MessageAttachmentRead(method="download_slack_file_bytes", parameters={"file_ref": reference})
        return None
