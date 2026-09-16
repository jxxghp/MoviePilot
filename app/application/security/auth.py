import copy
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Optional, Protocol, cast

from app.application.configuration import get_api_runtime_config_snapshot, get_chain_runtime_config_snapshot
from app.application.security.token import create_access_token
from app.application.security.user import FrozenJson
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.foundation.singleton import Singleton
from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.user import UserPermissions


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
            self._tickets[ticket] = {
                "user_id": int(user_id),
                "provider_id": provider_id,
                "metadata": copy.deepcopy(metadata) if metadata is not None else {},
                "created_at": now,
            }
            self._cleanup(now)
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
        return copy.deepcopy(data)

    def _cleanup(self, now: Optional[float] = None) -> None:
        """
        清理过期或过量的票据缓存。

        :param now: 当前时间戳，未传入时自动读取
        """
        current = time.time() if now is None else now
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

    @property
    def id(self) -> int:
        """返回用户 ID。"""

    @property
    def name(self) -> str:
        """返回用户名。"""

    @property
    def is_active(self) -> bool:
        """返回账号启用状态。"""

    @property
    def is_superuser(self) -> bool:
        """返回超级用户状态。"""

    @property
    def avatar(self) -> Optional[str]:
        """返回用户头像。"""

    @property
    def permissions(self) -> Mapping[str, FrozenJson]:
        """返回只读权限快照。"""


class AuthUserRepository(Protocol):
    """认证服务的用户数据端口。"""

    def get_by_name(self, name: str) -> Optional[AuthUser]:
        """按用户名查询用户。"""

    def get_by_id(self, user_id: int) -> Optional[AuthUser]:
        """按 ID 查询用户。"""

    def get_active_superuser(self) -> Optional[AuthUser]:
        """返回按稳定顺序选出的启用超级管理员。"""


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
        self._superuser_binding_name: str | None = None
        self._superuser_binding_id: int | None = None

    def get_user_by_id(self, user_id: int) -> Optional[AuthUser]:
        """按 ID 查询本地用户。"""
        return self._users.get_by_id(user_id)

    def has_passkey(self) -> bool:
        """判断系统是否已有 PassKey。"""
        return bool(self._passkeys.list())

    def build_superuser_token_payload(self) -> _SchemaTokenPayload:
        """从持久化用户和站点认证状态构造超级用户令牌载荷。"""
        configured_name = str(
            get_chain_runtime_config_snapshot().superuser or ""
        ).strip()
        if (
            self._superuser_binding_id is not None
            and configured_name == self._superuser_binding_name
        ):
            # 配置保存用户名；持久化 ID 保证管理员改名不会让管理员级集成失效。
            user = self._users.get_by_id(self._superuser_binding_id)
        else:
            user = (
                self._users.get_by_name(configured_name)
                if configured_name
                else self._users.get_active_superuser()
            )
            if user:
                self._superuser_binding_name = configured_name
                self._superuser_binding_id = user.id
        if not user or not user.is_active or not user.is_superuser:
            if not configured_name:
                raise PermissionError(
                    "未配置 SUPERUSER，且数据库中没有可用超级管理员"
                )
            raise PermissionError(
                "SUPERUSER 对应用户不存在、未启用或非超级管理员"
            )
        return _SchemaTokenPayload(
            sub=user.id,
            username=user.name,
            super_user=user.is_superuser,
            level=SitesHelper().auth_level,
            purpose="authentication",
        )

    def validate_token_identity(self, payload: _SchemaTokenPayload) -> None:
        """按当前持久化用户状态校验令牌身份与权限声明。"""
        if payload.sub is None:
            raise PermissionError("用户不存在或已禁用")
        user = self._users.get_by_id(payload.sub)
        if not user or not user.is_active:
            raise PermissionError("用户不存在或已禁用")
        if payload.username != user.name or payload.super_user != user.is_superuser:
            raise PermissionError("令牌身份或权限上下文不匹配")

    def build_token_response(self, user: AuthUser) -> _SchemaToken:
        """使用统一逻辑构造登录 Token 响应。"""
        level = SitesHelper().auth_level
        config = get_api_runtime_config_snapshot()
        return _SchemaToken(
            access_token=create_access_token(
                userid=user.id,
                username=user.name,
                super_user=user.is_superuser,
                expires_delta=timedelta(minutes=config.access_token_expire_minutes),
                level=level,
            ),
            token_type="bearer",
            super_user=user.is_superuser,
            user_id=user.id,
            user_name=user.name,
            avatar=user.avatar,
            level=level,
            permissions=cast(UserPermissions, dict(user.permissions)),
        )


_configured_auth_service: AuthService | None = None


def configure_auth_service(service: AuthService) -> None:
    """由启动组合根登记认证应用服务。"""
    global _configured_auth_service
    _configured_auth_service = service


def reset_auth_service() -> None:
    """清除当前 lifespan 的认证应用服务。"""
    global _configured_auth_service
    _configured_auth_service = None


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


def validate_token_identity(payload: _SchemaTokenPayload) -> None:
    """使用启动组合根注入的认证服务校验当前令牌身份。"""
    _get_auth_service().validate_token_identity(payload)


def build_token_response(user: AuthUser) -> _SchemaToken:
    """使用启动组合根注入的认证服务构造登录 Token 响应。"""
    return _get_auth_service().build_token_response(user)
