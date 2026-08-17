"""
用户数据访问。

认证依赖（get_current_user 等八个）已迁至 app/api/deps.py——那是 HTTP 层的关注点，
产出 403/400 而非数据。本模块只保留 UserOper。

这里不为那八个名字留惰性转发，否则会把
app.db.oper.user -> app.api.deps -> app.application.security 这条边永久焊进依赖图，
让数据访问模块在静态分析里牵着整个鉴权栈。仓外插件的旧 ``app.db.user_oper`` 路径由
runtime 兼容映射指向 SDK 薄门面；canonical 数据访问模块仍只依赖模型，不承担兼容职责。
"""
from typing import List, Optional

from app.db.base import DbOper
from app.db.models.user import User


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
