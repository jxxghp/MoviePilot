"""
渠道授权管理 API 的输入和输出合同。
"""

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ChannelMemberDelete(BaseModel):
    """
    按界面读取的版本删除，已授权成员必须明确再次确认。
    """

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: float = Field(allow_inf_nan=False, description="界面读取的授权版本 UTC Unix 秒")
    confirm_approved: bool = Field(default=False, description="是否再次确认删除当前已授权成员")


class ChannelMemberBatchTarget(BaseModel):
    """
    一个被显式选中的不可变成员及其授权版本。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="成员稳定 ID")
    expected_updated_at: float = Field(allow_inf_nan=False, description="选择时读取的授权版本 UTC Unix 秒")


class ChannelMemberBatch(BaseModel):
    """
    一次事务内处理明确选择的成员；批量审批只授予普通成员角色。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "disable", "delete"] = Field(description="批量通过、拒绝、停用或删除")
    members: list[ChannelMemberBatchTarget] = Field(min_length=1, max_length=100, description="最多一百个不重复成员")
    scopes: list[Literal["search", "subscribe", "download", "schedule"]] = Field(
        default_factory=lambda: cast(
            list[Literal["search", "subscribe", "download", "schedule"]], ["search", "subscribe", "download"]
        ), description="批量审批明确授予的普通成员权限",
    )
    confirm_approved: bool = Field(default=False, description="是否再次确认删除所选已授权成员")

    @model_validator(mode="after")
    def validate_unique_members(self) -> "ChannelMemberBatch":
        """
        拒绝重复身份，防止在同一批次中二次变更或删除。
        """
        if len({member.id for member in self.members}) != len(self.members):
            raise ValueError("同一批次不能重复选择成员")
        return self


class ChannelMemberBatchResult(BaseModel):
    """
    返回同一事务已提交的更新快照和删除身份，不报告未提交的成功。
    """

    members: list[ChannelMember] = Field(description="完成状态变更的成员")
    deleted_ids: list[str] = Field(description="已经删除的成员 ID")
