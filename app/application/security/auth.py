import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Optional, Protocol

from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.application.security.token import create_access_token, get_password_hash
from app.runtime.config import settings
from app.application.configuration import get_api_runtime_config_snapshot, get_chain_runtime_config_snapshot
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.runtime.log import logger
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


class AuthIdentityRepository(Protocol):
    """第三方身份绑定查询与新增端口。"""

    def get_by_provider_external_id(self, provider: str, external_id: str) -> Optional[Any]:
        """按 (provider, external_id) 查已绑定的身份行，返回对象须带 user_id 属性。"""

    def bind(
        self,
        user_id: int,
        provider: str,
        external_id: str,
        display_name: Optional[str] = None,
    ) -> Any:
        """新增身份绑定；该身份已被其他用户占用时抛出该端口自定义的冲突异常。"""


class AuthUserProvisioningRepository(Protocol):
    """第三方登录自动建号所需的最小用户创建端口。"""

    def get_by_name(self, name: str) -> Optional[AuthUser]:
        """按用户名查询用户，用于生成不冲突的新用户名。"""

    def add(self, **kwargs: Any) -> None:
        """新增用户。"""


_configured_identity_repository: Optional[AuthIdentityRepository] = None
_configured_user_provisioning_repository: Optional[AuthUserProvisioningRepository] = None


def configure_auth_identity_ports(
    identities: AuthIdentityRepository,
    provisioning: AuthUserProvisioningRepository,
) -> None:
    """由启动组合根登记第三方身份绑定查询与自动建号所需的数据端口。"""
    global _configured_identity_repository, _configured_user_provisioning_repository
    _configured_identity_repository = identities
    _configured_user_provisioning_repository = provisioning


def _generate_unique_username(
    provider_id: str, external_id: str, display_name: Optional[str]
) -> str:
    """
    为自动建号生成不与现有用户名冲突的用户名。

    :param provider_id: 提供方标识
    :param external_id: 第三方侧的用户标识
    :param display_name: 第三方侧的显示名，取不到时退回 provider/external_id 拼接
    :return: 不与现有用户重名的用户名
    """
    base = (display_name or f"{provider_id}_{external_id}").strip() or external_id
    base = base[:60]
    candidate = base
    suffix = 2
    while _configured_user_provisioning_repository.get_by_name(candidate) is not None:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def resolve_or_create_user_id_for_identity(
    provider_id: str,
    external_id: str,
    *,
    display_name: Optional[str] = None,
) -> Optional[int]:
    """
    按第三方身份查已绑定的本项目用户；未绑定时按
    ``settings.AUTH_IDENTITY_AUTO_CREATE_USER`` 决定是否自动建号并完成绑定。

    首次第三方登录默认不自动创建本项目用户：否则任何能登录该第三方账号的人都能在
    本项目开号，绑定必须由已登录用户主动发起。

    :param provider_id: 提供方标识
    :param external_id: 第三方侧的用户标识
    :param display_name: 第三方侧的显示名，仅在自动建号时用于生成用户名
    :return: 已绑定或新建的本项目用户 ID；未绑定且未开启自动建号时为 None
    :raises RuntimeError: 身份绑定端口尚未由组合根配置
    """
    if _configured_identity_repository is None:
        raise RuntimeError("第三方身份绑定端口尚未配置")
    existing = _configured_identity_repository.get_by_provider_external_id(
        provider_id, external_id
    )
    if existing is not None:
        return existing.user_id
    if not settings.AUTH_IDENTITY_AUTO_CREATE_USER:
        return None
    if _configured_user_provisioning_repository is None:
        raise RuntimeError("自动建号所需的用户创建端口尚未配置")
    logger.warning(
        f"第三方登录 {provider_id}（账号 {external_id}）尚未绑定本项目用户，"
        f"AUTH_IDENTITY_AUTO_CREATE_USER 已开启，将自动创建新用户——"
        f"任何能登录该第三方账号的人都会因此获得一个本项目账号，请确认该认证源可信"
    )
    username = _generate_unique_username(provider_id, external_id, display_name)
    _configured_user_provisioning_repository.add(
        name=username,
        is_active=True,
        is_superuser=False,
        hashed_password=get_password_hash(secrets.token_urlsafe(32)),
    )
    created = _configured_user_provisioning_repository.get_by_name(username)
    if created is None:
        return None
    identity = _configured_identity_repository.bind(
        created.id, provider_id, external_id, display_name
    )
    return identity.user_id


def create_plugin_auth_ticket_for_identity(
    provider_id: str,
    external_id: str,
    *,
    display_name: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """
    第三方登录成功后，查已绑定的本项目用户并签发一次性登录票据。

    这是插件完成自身的第三方认证握手后应调用的落点：查绑定命中即签发票据，
    未命中按 `resolve_or_create_user_id_for_identity` 的策略处理。

    ``provider_id`` 原样落进身份绑定表的 ``provider`` 列，宿主不改写也不校验它属于
    哪个登录入口——该列是绑定唯一键的一半，宿主替插件改口径就是替用户丢绑定。插件应
    原样回传登录页交来的入口标识（登录入口列表里的 ``id``），不要自行拼接：两个入口
    共用一个取值，就是两台服务器的账号落进同一个身份命名空间。

    :param provider_id: 登录入口标识，即身份绑定表 provider 列的取值
    :param external_id: 第三方侧的用户标识
    :param display_name: 第三方侧的显示名
    :param metadata: 插件侧附加信息，随票据一并保存
    :return: 一次性登录票据；未绑定且未开启自动建号时为 None，调用方应提示用户
        先登录后在设置页发起绑定
    """
    user_id = resolve_or_create_user_id_for_identity(
        provider_id, external_id, display_name=display_name
    )
    if user_id is None:
        return None
    return create_plugin_auth_ticket(user_id=user_id, provider_id=provider_id, metadata=metadata)


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
        user = self._users.get_by_name(get_chain_runtime_config_snapshot().superuser)
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
        config = get_api_runtime_config_snapshot()
        show_wizard = (
            not self._config.get(SystemConfigKey.SetupWizardState)
            and not config.advanced_mode
        )
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
