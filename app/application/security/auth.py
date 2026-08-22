import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Optional, Protocol

from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.application.security.token import create_access_token
from app.runtime.config import settings
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.schemas.types import SystemConfigKey
from app.foundation.singleton import Singleton


class AuthTicketStore(metaclass=Singleton):
    """
    插件认证一次性票据存储。
    """

    _ttl_seconds = 120
    _max_items = 1024

    def __init__(self):
        """
        初始化内存票据缓存。
        """
        self._tickets: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, user_id: int, provider_id: str, metadata: Optional[dict[str, Any]] = None) -> str:
        """
        创建短时一次性登录票据。

        :param user_id: 已通过插件认证的本地用户 ID
        :param provider_id: 认证提供方 ID
        :param metadata: 插件侧附加信息
        :return: 一次性票据字符串
        """
        ticket = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._cleanup(now)
            self._tickets[ticket] = {
                "user_id": int(user_id),
                "provider_id": provider_id,
                "metadata": metadata or {},
                "created_at": now,
            }
        return ticket

    def consume(self, ticket: str) -> Optional[dict[str, Any]]:
        """
        消费并删除一次性登录票据。

        :param ticket: 登录票据
        :return: 票据数据，票据不存在或过期时返回 None
        """
        if not ticket:
            return None
        now = time.time()
        with self._lock:
            data = self._tickets.pop(ticket, None)
            self._cleanup(now)
        if not data:
            return None
        if now - float(data.get("created_at") or 0) > self._ttl_seconds:
            return None
        return data

    def _cleanup(self, now: Optional[float] = None) -> None:
        """
        清理过期或过量的票据缓存。

        :param now: 当前时间戳，未传入时自动读取
        """
        current = now or time.time()
        expired = [
            key
            for key, value in self._tickets.items()
            if current - float(value.get("created_at") or 0) > self._ttl_seconds
        ]
        for key in expired:
            self._tickets.pop(key, None)
        if len(self._tickets) <= self._max_items:
            return
        ordered = sorted(
            self._tickets.items(),
            key=lambda item: float(item[1].get("created_at") or 0),
        )
        for key, _ in ordered[: len(self._tickets) - self._max_items]:
            self._tickets.pop(key, None)


def create_plugin_auth_ticket(user_id: int, provider_id: str, metadata: Optional[dict[str, Any]] = None) -> str:
    """
    为插件认证成功的用户创建一次性登录票据。

    :param user_id: 本地用户 ID
    :param provider_id: 认证提供方 ID
    :param metadata: 插件侧附加信息
    :return: 一次性票据字符串
    """
    return AuthTicketStore().create(user_id=user_id, provider_id=provider_id, metadata=metadata)


def consume_plugin_auth_ticket(ticket: str) -> Optional[dict[str, Any]]:
    """
    消费插件认证登录票据。

    :param ticket: 登录票据
    :return: 票据数据，票据不存在或过期时返回 None
    """
    return AuthTicketStore().consume(ticket)


class AuthUser(Protocol):
    """认证服务需要的最小用户投影。"""

    id: int
    name: str
    is_active: bool
    is_superuser: bool
    avatar: Optional[str]
    permissions: Optional[dict]


class AuthUserRepository(Protocol):
    """认证服务的用户数据端口。"""

    def get_by_name(self, name: str) -> Optional[AuthUser]:
        """按用户名查询用户。"""

    def get_by_id(self, user_id: int) -> Optional[AuthUser]:
        """按 ID 查询用户。"""


class AuthPasskeyRepository(Protocol):
    """认证提供方查询端口。"""

    def list(self) -> list[Any]:
        """返回已启用的 PassKey。"""


class AuthConfigRepository(Protocol):
    """认证配置读取端口。"""

    def get(self, key: Any) -> Any:
        """读取配置值。"""


class AuthService:
    """认证应用服务，编排用户、配置和 PassKey 端口。"""

    def __init__(
        self,
        users: AuthUserRepository,
        config: AuthConfigRepository,
        passkeys: AuthPasskeyRepository,
    ) -> None:
        """注入认证所需的数据端口。"""
        self._users = users
        self._config = config
        self._passkeys = passkeys

    def get_user_by_id(self, user_id: int) -> Optional[AuthUser]:
        """按 ID 查询本地用户。"""
        return self._users.get_by_id(user_id)

    def has_passkey(self) -> bool:
        """判断系统是否已有 PassKey。"""
        return bool(self._passkeys.list())

    def build_superuser_token_payload(self) -> _SchemaTokenPayload:
        """从持久化用户和站点认证状态构造超级用户令牌载荷。"""
        user = self._users.get_by_name(settings.SUPERUSER)
        if not user or not user.is_superuser:
            raise PermissionError("用户权限不足")
        return _SchemaTokenPayload(
            sub=user.id,
            username=user.name,
            super_user=user.is_superuser,
            level=SitesHelper().auth_level,
            purpose="authentication",
        )

    def build_token_response(self, user: AuthUser) -> _SchemaToken:
        """使用统一逻辑构造登录 Token 响应。"""
        level = SitesHelper().auth_level
        show_wizard = (
            not self._config.get(SystemConfigKey.SetupWizardState)
            and not settings.ADVANCED_MODE
        )
        return _SchemaToken(
            access_token=create_access_token(
                userid=user.id,
                username=user.name,
                super_user=user.is_superuser,
                expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
                level=level,
            ),
            token_type="bearer",
            super_user=user.is_superuser,
            user_id=user.id,
            user_name=user.name,
            avatar=user.avatar,
            level=level,
            permissions=user.permissions or {},
            wizard=show_wizard,
        )


_configured_auth_service: AuthService | None = None


def configure_auth_service(service: AuthService) -> None:
    """由启动组合根登记认证应用服务。"""
    global _configured_auth_service
    _configured_auth_service = service


def _get_auth_service() -> AuthService:
    """返回启动阶段登记的认证应用服务。"""
    if _configured_auth_service is None:
        raise RuntimeError("认证服务尚未配置")
    return _configured_auth_service


def get_configured_auth_service() -> AuthService:
    """返回启动阶段登记的认证服务。"""
    return _get_auth_service()


def build_superuser_token_payload() -> _SchemaTokenPayload:
    """使用启动组合根注入的认证服务构造超级用户令牌载荷。"""
    return _get_auth_service().build_superuser_token_payload()


def build_token_response(user: AuthUser) -> _SchemaToken:
    """使用启动组合根注入的认证服务构造登录 Token 响应。"""
    return _get_auth_service().build_token_response(user)
