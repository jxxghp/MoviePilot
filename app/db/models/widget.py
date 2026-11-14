"""
小组件专用凭据记录；主登录资料与原始 secret 均不落入该表。
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class WidgetRegistration(Base):
    """
    每用户每安装最多一项授权，删除用户时级联移除，身份重建另有 owner 校验。
    """

    id = get_id_column()
    # 随机登记 UUID，不使用递增主键作为外部身份。
    registration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 本机安装 UUID；同账号同安装的轮换只占一个名额。
    installation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 当前账号主键，外键不代替不可变身份校验。
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    # 数据库分配的不可变身份，阻止删除重建与主键复用。
    owner: Mapped[str] = mapped_column(String(32), nullable=False)
    # 完整专用 Bearer 的 SHA-256 十六进制摘要。
    secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # UTC Unix 秒，有效期只由服务端决定。
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("registration_id", name="uq_widgetregistration_registration"),
        UniqueConstraint("user_id", "installation_id", name="uq_widgetregistration_installation"),
        Index("ix_widgetregistration_owner", "owner"),
    )
