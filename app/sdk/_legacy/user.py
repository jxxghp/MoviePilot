"""兼容旧 ``app.db.user_oper`` 中混合的数据访问与认证依赖。"""

from app.application.security.dependencies import (
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
from app.db.models.user import User


__all__ = [
    "UserOper",
    "User",
    "get_current_active_manage_user",
    "get_current_active_manage_user_async",
    "get_current_active_superuser",
    "get_current_active_superuser_async",
    "get_current_active_user",
    "get_current_active_user_async",
    "get_current_user",
    "get_current_user_async",
]
