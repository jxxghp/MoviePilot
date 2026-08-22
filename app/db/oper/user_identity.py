"""第三方身份绑定数据访问。"""
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.user_identity import UserIdentity


class UserIdentityAlreadyBoundError(Exception):
    """该第三方身份已绑定到其他本项目用户，无法重复绑定。"""


class UserIdentityOper(DbOper):
    """封装第三方身份绑定的查询、新增与解绑，避免调用方直接处理唯一约束冲突。"""

    def get_by_provider_external_id(
        self, provider: str, external_id: str
    ) -> Optional[UserIdentity]:
        """按 (provider, external_id) 查已绑定的身份行。"""
        return UserIdentity.get_by_provider_external_id(self._db, provider, external_id)

    async def async_get_by_provider_external_id(
        self, provider: str, external_id: str
    ) -> Optional[UserIdentity]:
        """异步按 (provider, external_id) 查已绑定的身份行。"""
        return await UserIdentity.async_get_by_provider_external_id(
            self._db, provider, external_id
        )

    def list_by_user_id(self, user_id: int) -> List[UserIdentity]:
        """列出指定用户的全部身份绑定。"""
        return UserIdentity.get_by_user_id(self._db, user_id)

    async def async_list_by_user_id(self, user_id: int) -> List[UserIdentity]:
        """异步列出指定用户的全部身份绑定。"""
        return await UserIdentity.async_get_by_user_id(self._db, user_id)

    def bind(
        self,
        user_id: int,
        provider: str,
        external_id: str,
        display_name: Optional[str] = None,
    ) -> UserIdentity:
        """
        新增身份绑定；该身份已绑定同一用户时直接返回既有记录（幂等）。

        :param user_id: 本项目用户 ID
        :param provider: 提供方标识
        :param external_id: 第三方侧的用户标识
        :param display_name: 第三方侧的显示名
        :return: 绑定后的身份行
        :raises UserIdentityAlreadyBoundError: 该第三方身份已绑定其他本项目用户
        """
        existing = self.get_by_provider_external_id(provider, external_id)
        if existing is not None:
            if existing.user_id == user_id:
                return existing
            raise UserIdentityAlreadyBoundError(
                f"该 {provider} 账号已绑定到其他用户，无法重复绑定"
            )
        identity = UserIdentity(
            user_id=user_id,
            provider=provider,
            external_id=external_id,
            display_name=display_name,
        )

        def stage(session: Session) -> None:
            """在调用方事务中暂存身份绑定并立即 flush，使唯一约束冲突在本方法内可见。"""
            session.add(identity)
            session.flush()

        try:
            self._execute_sync_write(stage)
        except IntegrityError as error:
            raise UserIdentityAlreadyBoundError(
                f"该 {provider} 账号已绑定到其他用户，无法重复绑定"
            ) from error
        # flush 已分配主键并把字段写入当前事务，直接返回即可；是否提交由调用方
        # （无显式会话时是本方法委托的兼容事务）决定，不在这里替调用方提前收尾。
        return identity

    def unbind(self, identity_id: int, user_id: int) -> bool:
        """解绑指定用户名下的身份绑定，不属于该用户时返回 False。"""
        return bool(UserIdentity.delete_by_id(self._db, identity_id, user_id))

    async def async_unbind(self, identity_id: int, user_id: int) -> bool:
        """异步解绑指定用户名下的身份绑定，不属于该用户时返回 False。"""
        return bool(await UserIdentity.async_delete_by_id(self._db, identity_id, user_id))
