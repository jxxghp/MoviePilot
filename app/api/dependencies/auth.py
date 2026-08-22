"""用户身份、授权与认证服务依赖。

身份解析与权限判定只有一份实现，落在 canonical 的 ``app.application.security``；
本模块只做转出，让 HTTP 层与 ``app.sdk`` 交给插件的是同一批函数对象。同名函数各写一份
会让 ``Depends`` 按可调用对象缓存的子依赖各算一次，同一请求里重复读库，
且两处权限判定可以各自漂移而没有任何地方发现。

本模块自己拥有的是认证域的服务工厂：它们从 ``HostRuntime`` 的命名领域取仓储，
不经字符串键的全局服务表。
"""

from typing import cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.context import get_async_session, get_host_runtime
from app.application.security.auth import (
    AuthConfigRepository,
    AuthPasskeyRepository,
    AuthService,
    AuthUserRepository,
)
from app.application.security.dependencies import (  # noqa: F401
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_current_active_user_async,
    get_current_user,
    get_current_user_async,
)
from app.application.security.passkeys import PasskeyRepository, PasskeyService
from app.application.security.user import (
    AsyncUnitOfWork,
    UserRepository,
    UserService,
)
from app.startup.ports.context import HostRuntime


def get_user_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> UserService:
    """组装用户管理应用服务。"""
    return UserService(
        repository=cast(
            UserRepository, runtime.authentication.user_repository(db)
        ),
        unit_of_work=cast(
            AsyncUnitOfWork, runtime.persistence.async_transaction(db)
        ),
    )


def get_auth_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> AuthService:
    """组装同步认证应用服务。"""
    return AuthService(
        users=cast(AuthUserRepository, runtime.authentication.standalone_user()),
        config=cast(AuthConfigRepository, runtime.authentication.system_config()),
        passkeys=cast(AuthPasskeyRepository, runtime.authentication.passkey()),
    )


def get_passkey_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> PasskeyService:
    """组装 PassKey 应用服务。"""
    return PasskeyService(repository=cast(
        PasskeyRepository, runtime.authentication.passkey()
    ))
