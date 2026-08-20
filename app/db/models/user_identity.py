from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import async_db_query, async_db_update, db_query, db_update


class UserIdentity(Base):
    """
    第三方身份绑定表

    一个本项目用户可绑定多个第三方身份（GitHub 账号、不同媒体服务器各自的账号等），
    ``(provider, external_id)`` 唯一，禁止同一第三方身份绑定到多个本项目用户；不对
    ``(user_id, provider)`` 设唯一约束，因为同一用户允许绑定同一 provider 族下的
    多个实例（例如两台媒体服务器）。

    ``provider`` 取值为认证扩展的 ``AuthProviderDeclaration.id``，插件未显式声明时
    宿主回落为 ``plugin:<插件实例键>``，天然带实例粒度。
    """
    # ID
    id = get_id_column()
    # 本项目用户 ID，用户删除时级联删除其全部身份绑定
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE", name="fk_useridentity_user_id"),
        nullable=False,
        index=True,
    )
    # 提供方标识，即 AuthProviderDeclaration.id 或其缺省回落值 plugin:<实例键>
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # 第三方侧的用户标识
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    # 第三方侧的显示名，供用户在界面上认出绑定的是哪个账号
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 创建时间
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "provider", "external_id", name="ux_useridentity_provider_external_id"
        ),
    )

    @classmethod
    @db_query
    def get_by_provider_external_id(cls, db: Session, provider: str, external_id: str):
        """按 (provider, external_id) 查已绑定的身份行"""
        return db.execute(
            select(cls).where(cls.provider == provider, cls.external_id == external_id)
        ).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_provider_external_id(
        cls, db: AsyncSession, provider: str, external_id: str
    ):
        """异步按 (provider, external_id) 查已绑定的身份行"""
        result = await db.execute(
            select(cls).where(cls.provider == provider, cls.external_id == external_id)
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_id(cls, db: Session, identity_id: int):
        """按 ID 查身份绑定行"""
        return db.execute(select(cls).where(cls.id == identity_id)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_id(cls, db: AsyncSession, identity_id: int):
        """异步按 ID 查身份绑定行"""
        result = await db.execute(select(cls).where(cls.id == identity_id))
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_user_id(cls, db: Session, user_id: int) -> List["UserIdentity"]:
        """列出指定用户的全部身份绑定"""
        return list(
            db.execute(select(cls).where(cls.user_id == user_id)).scalars().all()
        )

    @classmethod
    @async_db_query
    async def async_get_by_user_id(cls, db: AsyncSession, user_id: int) -> List["UserIdentity"]:
        """异步列出指定用户的全部身份绑定"""
        result = await db.execute(select(cls).where(cls.user_id == user_id))
        return list(result.scalars().all())

    @classmethod
    @db_update
    def delete_by_id(cls, db: Session, identity_id: int, user_id: int):
        """删除指定用户名下的身份绑定，不属于该用户时不做任何事"""
        identity = db.execute(
            select(cls).where(cls.id == identity_id, cls.user_id == user_id)
        ).scalars().first()
        if identity:
            identity.delete(db, identity.id)
            return True
        return False

    @classmethod
    @async_db_update
    async def async_delete_by_id(cls, db: AsyncSession, identity_id: int, user_id: int):
        """异步删除指定用户名下的身份绑定，不属于该用户时不做任何事"""
        result = await db.execute(
            select(cls).where(cls.id == identity_id, cls.user_id == user_id)
        )
        identity = result.scalars().first()
        if identity:
            await identity.async_delete(db, identity.id)
            return True
        return False

    @classmethod
    @db_update
    def delete_by_user_id(cls, db: Session, user_id: int):
        """删除指定用户的全部身份绑定，供用户删除时级联清理"""
        db.execute(delete(cls).where(cls.user_id == user_id))

    @classmethod
    @async_db_update
    async def async_delete_by_user_id(cls, db: AsyncSession, user_id: int):
        """异步删除指定用户的全部身份绑定，供用户删除时级联清理"""
        await db.execute(delete(cls).where(cls.user_id == user_id))
