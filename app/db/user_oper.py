"""
用户数据访问。

模块级的认证依赖（get_current_user 等）已迁至 app/api/deps.py——那是 HTTP 层的
关注点，产出 403/400 而非数据。本模块只保留 UserOper。

旧导入路径经下方 __getattr__ 继续可用：第三方插件在仓库外、不可枚举，直接移除
会让它们在 import 期崩。用惰性转发而不是模块级 re-import，是为了避免
app.db.user_oper -> app.api.deps -> app.db 这条链在导入期成环。
"""
from typing import Any, List, Optional

from app.db import DbOper
from app.db.models.user import User

# 已迁至 app/api/deps.py 的认证依赖，保留旧路径兼容
_MOVED_TO_API_DEPS = (
    "get_current_user",
    "get_current_user_async",
    "get_current_active_user",
    "get_current_active_user_async",
    "get_current_active_manage_user",
    "get_current_active_manage_user_async",
    "get_current_active_superuser",
    "get_current_active_superuser_async",
)


def __getattr__(name: str) -> Any:
    """
    兼容旧导入路径：认证依赖已迁往 app.api.deps，按需转发。
    :param name: 属性名
    :return: 迁移后的目标对象
    """
    if name in _MOVED_TO_API_DEPS:
        from app.api import deps

        return getattr(deps, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class UserOper(DbOper):
    """
    用户管理
    """

    def list(self) -> List[User]:
        """
        获取用户列表
        """
        return User.list(self._db)

    def add(self, **kwargs):
        """
        新增用户
        """
        user = User(**kwargs)
        user.create(self._db)

    def get_by_name(self, name: str) -> Optional[User]:
        """
        根据用户名获取用户
        """
        return User.get_by_name(self._db, name)

    async def async_get_by_name(self, name: str) -> Optional[User]:
        """
        异步根据用户名获取用户。
        """
        return await User.async_get_by_name(self._db, name)

    async def async_get_by_id(self, user_id: int) -> Optional[User]:
        """
        异步根据用户 ID 获取用户。
        """
        return await User.async_get_by_id(self._db, user_id)

    def get_permissions(self, name: str) -> dict:
        """
        获取用户权限
        """
        user = User.get_by_name(self._db, name)
        if user:
            return user.permissions or {}
        return {}

    def get_settings(self, name: str) -> Optional[dict]:
        """
        获取用户个性化设置，返回None表示用户不存在
        """
        user = User.get_by_name(self._db, name)
        if user:
            return user.settings or {}
        return None

    def get_setting(self, name: str, key: str) -> Optional[str]:
        """
        获取用户个性化设置
        """
        settings = self.get_settings(name)
        if settings:
            return settings.get(key)
        return None

    def get_name(self, **kwargs) -> Optional[str]:
        """
        根据绑定账号获取用户名称
        """
        users = self.list()
        for user in users:
            user_setting = user.settings
            if user_setting:
                for k, v in kwargs.items():
                    if user_setting.get(k) == str(v):
                        return user.name
        return None
