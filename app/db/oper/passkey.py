"""PassKey 数据访问适配器。"""

from typing import Any, Optional

from app.db.base import DbOper
from app.db.models.passkey import PassKey


class PassKeyOper(DbOper):
    """封装 PassKey 查询和维护，避免 API 层直接引用模型静态方法。"""

    def list_by_user_id(self, user_id: int) -> list[PassKey]:
        """读取用户启用的 PassKey。"""
        return PassKey.get_by_user_id(self._db, user_id)

    def list(self) -> list[PassKey]:
        """读取全部 PassKey，用于判断系统是否已配置通行密钥。"""
        return PassKey.list(self._db)

    def get_by_credential_id(self, credential_id: str) -> Optional[PassKey]:
        """按凭证 ID 读取启用的 PassKey。"""
        return PassKey.get_by_credential_id(self._db, credential_id)

    def create(self, payload: dict[str, Any]) -> PassKey:
        """创建 PassKey 凭证。"""
        passkey = PassKey(**payload)
        passkey.create(self._db)
        return passkey

    def update_last_used(self, passkey: PassKey, sign_count: int) -> bool:
        """更新凭证最后使用时间和签名计数。"""
        return bool(passkey.update_last_used(self._db, sign_count))

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除指定用户的凭证。"""
        return bool(PassKey.delete_by_id(self._db, passkey_id, user_id))
