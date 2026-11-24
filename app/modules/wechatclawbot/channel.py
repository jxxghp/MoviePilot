"""
WechatClawBot 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from typing import Any

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelConnection,
    ChannelConnectionInfo,
    ChannelPlatformCapabilities,
)
from app.schemas.message import MessageAttachmentRead


class WechatClawBotPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "wechatclawbot"
    channel = "微信ClawBot"
    attachment_schemes = ("wxclaw",)
    private_forbidden_prefixes = ("group:", "gid#")
    private_forbidden_suffixes = ("@chatroom",)
    interaction = True
    cache_reconciliation = True
    binding_fields = ("wechatclawbot_userid",)
    edit_fields = ("WECHATCLAWBOT_DEFAULT_TARGET", "WECHATCLAWBOT_POLL_TIMEOUT")
    public_fields = frozenset({"WECHATCLAWBOT_BASE_URL"})
    write_only = frozenset()
    default_targets = (("WECHATCLAWBOT_DEFAULT_TARGET", "user"),)
    bound_targets = (("wechatclawbot_userid", "user", True),)
    target_patterns = {}
    needs_target_review = True
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user",), target_kinds=("bound", "user"), login_management=True
    )

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        只对官方 iLink 服务声明已认证的个人事件能力。
        """
        trusted = connection.provider_config.get("WECHATCLAWBOT_BASE_URL") in (
            None,
            "",
            "https://ilinkai.weixin.qq.com",
            "https://ilinkai.weixin.qq.com/",
        )
        return self.capabilities_model.model_copy(update={"rule_kinds": ("user",) if trusted else ()})

    def reconcile(
        self, previous: tuple[ChannelConnectionInfo, ...], current: tuple[ChannelConnectionInfo, ...]
    ) -> None:
        """
        对账此模块拥有的登录缓存，失败让宿主报告已保存但未同步。
        """
        from app.modules.wechatclawbot.wechatclawbot import WechatClawBot

        before = [{"id": item.id, "name": item.name, "type": item.provider} for item in previous]
        after = [{"id": item.id, "name": item.name, "type": item.provider} for item in current]
        if before == after:
            return
        if not WechatClawBot.reconcile_cached_states(before, after).get("success"):
            raise RuntimeError("ClawBot 登录缓存未同步")

    def manage(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """
        已保存连接可管理本模块登录状态；其它动作仍由标准模块入口处理。
        """
        if params.get("saved_connection") is not True or action not in {
            "status",
            "refresh_qrcode",
            "logout",
            "test_connection",
        }:
            return None
        from app.modules.wechatclawbot import WechatClawBotModule
        from app.schemas.types import NotificationAction, NotificationChannel

        return WechatClawBotModule().channel_manage(
            channel=NotificationChannel.WechatClawBot, action=NotificationAction(action), **params
        )

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        解释本平台的附件引用并复用既有下载方法，其他平台的引用不进入此连接。
        """
        if reference.startswith("wxclaw://image/") and kind in {"image", "file"}:
            return MessageAttachmentRead(
                method="download_wechat_image_to_data_url", parameters={"image_ref": reference}, encoding="data_url"
            )
        if kind in {"audio", "file"} and reference.startswith(("wxclaw://voice/", "wxclaw://file/")):
            return MessageAttachmentRead(
                method="download_wechat_media_bytes", parameters={"media_ref": reference}, audio_filename="input.amr"
            )
        return None
