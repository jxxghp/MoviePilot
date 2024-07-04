import json
from typing import Optional

from app.db import DbOper
from app.db.models.user import User


class UserOper(DbOper):
    """
    用户管理
    """

    def get_permissions(self, name: str) -> dict:
        """
        获取用户权限
        """
        user = User.get_by_name(self._db, name)
        if user:
            try:
                return json.loads(user.permissions)
            except json.JSONDecodeError:
                return {}
        return {}

    def get_settings(self, name: str) -> Optional[dict]:
        """
        获取用户个性化设置，返回None表示用户不存在
        """
        user = User.get_by_name(self._db, name)
        if user:
            try:
                if user.settings:
                    return json.loads(user.settings)
                return {}
            except json.JSONDecodeError:
                return {}
        return None

    def get_setting(self, name: str, key: str) -> Optional[str]:
        """
        获取用户个性化设置
        """
        settings = self.get_settings(name)
        if settings:
            return settings.get(key)
        return None
