"""
统一渠道成员身份、申请状态和授权范围的 ORM 记录。
"""

from sqlalchemy import JSON, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChannelAuthorization(Base):
    """
    按不可变通知实例与平台发送者唯一隔离的授权记录。
    """

    __tablename__ = "channelauthorization"
    # 随机成员身份，不随昵称或渠道改名变化。
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 不可变通知配置身份，不对显示名称建立关联。
    notification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 平台渠道名称与完整发送者标识。
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    sender_id: Mapped[str] = mapped_column(String(256), nullable=False)
    # 昵称只供管理员核对，不参与认证。
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 申请、批准、拒绝和停用状态。
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 成员或管理员角色及普通成员能力集合。
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 固定业务归属与可选的已验证网页登录账号。
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 业务账号不可变身份，防止删除后主键复用继承旧授权。
    user_identity: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_user_id: Mapped[int | None] = mapped_column(Integer)
    # 仅由平台已验证报文登记的同一发送者别名，如飞书 user_id 与 open_id。
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 审计时间使用 UTC Unix 秒，来源用于解释旧名单迁移。
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    # 最近审批或变更权限的系统管理员主键，旧迁移记录为空。
    reviewed_by: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ux_channel_authorization_sender", "notification_id", "sender_id", unique=True),)


class ChannelBinding(Base):
    """
    网页账号发起的短时一次性渠道关联凭据，只保存随机码摘要。
    """

    __tablename__ = "channelbinding"
    # 随机码 SHA-256 摘要，原文只在生成时返回给已登录用户。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 网页端确认的本地账号及不可变身份。
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_identity: Mapped[str] = mapped_column(String(32), nullable=False)
    # UTC Unix 秒，到期或消费后不可再关联任何渠道。
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)


class ChannelInboundReceipt(Base):
    """
    主机已认领的可信入站事件，按短时窗口保留连接和事件 ID 的组合摘要。
    """

    __tablename__ = "channelinboundreceipt"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)


class ChannelRuleTaskGrant(Base):
    """
    规则定时任务的权限快照，关联原成员并绑定审批和策略版本。
    """

    __tablename__ = "channelruletaskgrant"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    member_version: Mapped[float] = mapped_column(Float, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
