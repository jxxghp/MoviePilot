"""兼容旧 ``app.db.user_oper`` 中混合的数据访问与认证依赖。"""

from app.api.deps import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_current_active_user_async,
    get_current_user,
    get_current_user_async,
)
from app.db.oper.user import UserOper


__all__ = [
    "UserOper",
    "get_current_active_manage_user",
    "get_current_active_manage_user_async",
    "get_current_active_superuser",
    "get_current_active_superuser_async",
    "get_current_active_user",
    "get_current_active_user_async",
    "get_current_user",
    "get_current_user_async",
]
