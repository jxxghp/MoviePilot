"""
持久化已知账号的浏览器推送订阅，不导入历史无归属内存记录。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class WebPushSubscription(Base):
    """
    以端点摘要唯一标识设备，账号删除时级联清理并保留不可变身份校验。

    :ivar id: 内部自增主键，不用于账号或设备归属
    :ivar endpoint_digest: 完整端点的 SHA-256 摘要，避免长 URL 唯一索引限制
    :ivar endpoint: 未改写的完整推送地址
    :ivar p256dh: 浏览器公钥
    :ivar auth: 浏览器认证密钥
    :ivar user_id: 仅用于级联清理的账号外键
    :ivar owner: 不可变账号通知身份，不能用主键或用户名代替
    :ivar revision: 每次重新登记生成的随机版本
    """

    id = get_id_column()
    endpoint_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)
    auth: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    owner: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_webpushsubscription_owner", "owner"),)
