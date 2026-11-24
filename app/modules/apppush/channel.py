"""
AppPush 的身份、配置与地址协议扩展；不决定成员角色、审批或业务权限。
"""

from app.modules._base.channel import ChannelPlatformBase
from app.schemas.channel import (
    ChannelPlatformCapabilities,
)


class AppPushPlatform(ChannelPlatformBase):
    """
    由模块能力清单注册的平台事实及协议实现。
    """

    provider = "apppush"
    channel = "AppPush"
    interaction = False
    binding_fields = ()
    edit_fields = ("PUSH_SERVER_URL", "PUSH_RELAY_KEY")
    public_fields = frozenset()
    write_only = frozenset({"PUSH_RELAY_KEY"})
    default_targets = ()
    bound_targets = ()
    target_patterns = {}
    needs_target_review = False
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=(), target_kinds=("bound", "account"), account_targets=True
    )
