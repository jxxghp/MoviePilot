"""
渠道授权管理 API 的输入和输出合同。
"""

from typing import Literal, cast

from pydantic import BaseModel, Field


class ChannelMember(BaseModel):
    """
    管理员弹窗展示的成员和申请，不暴露内部登录凭据。
    """

    id: str = Field(description="不可变渠道成员身份")
    notification_id: str = Field(description="不可变通知实例 ID")
    channel: str = Field(description="平台渠道名称")
    source: str = Field(description="当前通知实例显示名称，实例删除后保留原身份提示")
    sender_id: str = Field(description="完整平台发送者 ID")
    display_name: str = Field(description="仅用于核对的昵称")
    status: Literal["pending", "approved", "rejected", "disabled"] = Field(description="审批与启用状态")
    role: Literal["member", "admin"] = Field(description="普通成员或管理员")
    scopes: list[str] = Field(description="搜索、订阅、下载和定时任务能力集合")
    linked_username: str | None = Field(default=None, description="已验证关联的网页账号名称")
    origin: str = Field(description="request 为申请，legacy 为旧名单迁移，manual 为手工授权")
    created_at: float = Field(description="首次登记的 UTC Unix 秒")
    updated_at: float = Field(description="最近权限变动的 UTC Unix 秒")
    channel_enabled: bool = Field(description="通知实例当前是否存在且启用")


class ChannelMemberUpdate(BaseModel):
    """
    系统管理员明确提交的审批结果和权限。
    """

    status: Literal["approved", "rejected", "disabled"] = Field(description="批准、拒绝或停用")
    role: Literal["member", "admin"] = Field(default="member", description="角色；管理员必须由界面单独确认")
    scopes: list[Literal["search", "subscribe", "download", "schedule"]] = Field(
        default_factory=lambda: cast(
            list[Literal["search", "subscribe", "download", "schedule"]], ["search", "subscribe", "download"]
        ),
        description="普通成员可用能力",
    )


class ChannelMemberCreate(ChannelMemberUpdate):
    """
    管理员直接添加一个已核对的渠道发送者。
    """

    notification_id: str = Field(min_length=1, max_length=128, description="目标通知实例稳定 ID")
    sender_id: str = Field(min_length=1, max_length=256, description="完整平台发送者 ID，不能使用群 ID 或昵称")
    display_name: str = Field(default="", max_length=256, description="管理员备注的显示名称")


class AuthorizationChannel(BaseModel):
    """
    授权弹窗可选的通知实例，不包含渠道密钥或连接参数。
    """

    id: str = Field(description="不可变通知实例 ID")
    name: str = Field(description="通知实例名称")
    channel: str = Field(description="平台渠道名称")
    enabled: bool = Field(description="实例是否启用")


class ChannelAuthorizationData(BaseModel):
    """
    渠道授权弹窗首次加载的数据。
    """

    members: list[ChannelMember] = Field(description="成员与待审批记录")
    channels: list[AuthorizationChannel] = Field(description="可管理的通知实例")


class ChannelBindingData(BaseModel):
    """
    当前登录账号一次性关联渠道所需的短时凭据。
    """

    code: str = Field(description="只返回一次的高熵关联码，不应记录日志或发到群聊")
    expires_at: float = Field(description="关联码到期的 UTC Unix 秒")
