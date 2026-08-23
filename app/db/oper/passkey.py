"""PassKey 数据访问适配器。"""

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.passkey import (
    PassKey,
    _get_by_credential_id_statement,
    _get_by_user_id_statement,
)


class PassKeyOper(DbOper):
    """封装 PassKey 查询和维护，避免 API 层直接引用模型静态方法。"""

    def list_by_user_id(self, user_id: int) -> list[PassKey]:
        """读取用户启用的 PassKey。"""
        def query(session: Session) -> list[PassKey]:
            """在调用方会话中读取用户启用的 PassKey。"""
            return list(session.execute(
                _get_by_user_id_statement(PassKey, user_id)
            ).scalars().all())

        return self._execute_sync_query(query)

    def list(self) -> list[PassKey]:
        """读取全部 PassKey，用于判断系统是否已配置通行密钥。"""
        return self._execute_sync_query(
            lambda session: list(session.execute(select(PassKey)).scalars().all())
        )

    def get_by_credential_id(self, credential_id: str) -> Optional[PassKey]:
        """按凭证 ID 读取启用的 PassKey。"""
        def query(session: Session) -> Optional[PassKey]:
            """在调用方会话中按凭证 ID 读取启用的 PassKey。"""
            return session.execute(
                _get_by_credential_id_statement(PassKey, credential_id)
            ).scalars().first()

        return self._execute_sync_query(query)

    def create(self, payload: dict[str, Any]) -> PassKey:
        """创建 PassKey 凭证。"""
        passkey = PassKey(**payload)
        self._execute_sync_write(lambda session: self._stage_create(session, passkey))
        return passkey

    @staticmethod
    def _stage_create(session: Any, passkey: PassKey) -> None:
        """在调用方事务中暂存凭证并分配主键。"""
        session.add(passkey)
        session.flush()

    def update_last_used(self, passkey: PassKey, sign_count: int) -> bool:
        """更新凭证最后使用时间和签名计数。"""
        return bool(self._execute_sync_write(
            lambda session: passkey.update_last_used(session, sign_count)
        ))

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除指定用户的凭证。"""
        return bool(self._execute_sync_write(
            lambda session: PassKey.delete_by_id(session, passkey_id, user_id)
        ))

    async def async_delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """在独立异步事务中删除指定用户的凭证。"""
        return bool(await self._execute_async_write(
            lambda session: PassKey.async_delete_by_id(
                session, passkey_id, user_id
            )
        ))
