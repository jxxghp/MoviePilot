"""用户管理用例。

该模块承接用户端点需要的异步用户操作。具体数据库访问由请求组合根注入，
避免 API 层同时承担 HTTP 编排和 ORM 适配职责。
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, TypeVar, cast

FrozenJson: TypeAlias = (
    str | int | float | bool | None | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]
)
T = TypeVar("T")


def _freeze_json(value: Any) -> FrozenJson:
    """递归复制 JSON 值，阻止 ORM JSON 字段在会话外继续被修改。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return cast(FrozenJson, value)


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, FrozenJson]:
    """把可空 JSON 对象复制为只读映射。"""
    frozen = _freeze_json(value or {})
    return cast(Mapping[str, FrozenJson], frozen)


@dataclass(frozen=True, slots=True)
class UserSnapshot:
    """脱离数据库会话的只读用户资料与权限快照。"""

    id: int
    name: str
    email: str | None
    is_active: bool
    is_superuser: bool
    avatar: str | None
    is_otp: bool
    permissions: Mapping[str, FrozenJson]
    settings: Mapping[str, FrozenJson]

    @classmethod
    def build(
        cls,
        *,
        user_id: int,
        name: str,
        email: str | None,
        is_active: bool | None,
        is_superuser: bool | None,
        avatar: str | None,
        is_otp: bool | None,
        permissions: Mapping[str, Any] | None,
        settings: Mapping[str, Any] | None,
    ) -> "UserSnapshot":
        """复制持久化字段并构造不可变的公开用户快照。"""
        return cls(
            id=user_id,
            name=name,
            email=email,
            is_active=bool(is_active),
            is_superuser=bool(is_superuser),
            avatar=avatar,
            is_otp=bool(is_otp),
            permissions=_freeze_mapping(permissions),
            settings=_freeze_mapping(settings),
        )


@dataclass(frozen=True, slots=True)
class UserAuthSnapshot:
    """仅供认证链使用的只读用户凭据快照。"""

    user: UserSnapshot
    hashed_password: str | None
    otp_secret: str | None

    @property
    def id(self) -> int:
        """返回用户 ID。"""
        return self.user.id

    @property
    def name(self) -> str:
        """返回用户名。"""
        return self.user.name

    @property
    def is_active(self) -> bool:
        """返回账号启用状态。"""
        return self.user.is_active

    @property
    def is_superuser(self) -> bool:
        """返回超级用户状态。"""
        return self.user.is_superuser

    @property
    def avatar(self) -> str | None:
        """返回用户头像。"""
        return self.user.avatar

    @property
    def is_otp(self) -> bool:
        """返回 OTP 启用状态。"""
        return self.user.is_otp

    @property
    def permissions(self) -> Mapping[str, FrozenJson]:
        """返回只读权限快照。"""
        return self.user.permissions


@dataclass(frozen=True, slots=True)
class AuxiliaryUserCreate:
    """辅助认证首次落地本地用户所需的最小命令。"""

    name: str
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = False


class ChainUserRepository(Protocol):
    """用户 Chain 和 Agent 共享的类型化查询与创建端口。"""

    def get_auth_by_name(self, name: str) -> UserAuthSnapshot | None:
        """按用户名读取认证快照。"""

    async def async_get_by_name(self, name: str) -> UserSnapshot | None:
        """异步按用户名读取公开用户快照。"""

    def create_auxiliary(self, command: AuxiliaryUserCreate) -> UserAuthSnapshot:
        """原子创建辅助认证用户并返回已提交快照。"""

    def get_notification_settings(
        self,
        name: str,
    ) -> Mapping[str, FrozenJson] | None:
        """读取通知路由设置；用户不存在时返回空值。"""

    async def async_get_notification_settings(
        self,
        name: str,
    ) -> Mapping[str, FrozenJson] | None:
        """异步读取通知路由设置；用户不存在时返回空值。"""

    def find_name_by_bindings(self, bindings: Mapping[str, object]) -> str | None:
        """解析唯一启用用户的渠道绑定，歧义时拒绝归属。"""


class UserRepository(Protocol):
    """用户用例所需的最小异步数据端口。"""

    async def async_list(self) -> list[UserSnapshot]:
        """返回全部用户。"""

    async def async_get_by_name(self, name: str) -> UserSnapshot | None:
        """按用户名返回用户。"""

    async def async_get_by_id(self, user_id: int) -> UserSnapshot | None:
        """按用户 ID 返回用户。"""

    async def async_create(self, payload: dict[str, Any]) -> UserSnapshot | None:
        """创建用户并返回持久化对象。"""

    async def async_update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> UserSnapshot | None:
        """更新用户并返回原用户对象。"""

    async def async_delete(self, user_id: int) -> None:
        """删除用户。"""

    async def async_update_otp_by_name(self, name: str, otp: bool, secret: str) -> None:
        """更新用户 OTP 状态。"""


class AsyncUnitOfWork(Protocol):
    """用户写用例所需的异步事务边界。"""

    async def commit(self) -> None:
        """提交用户写入。"""

    async def rollback(self) -> None:
        """回滚失败的用户写入。"""


class UserService:
    """用户管理应用服务。"""

    def __init__(
        self,
        repository: UserRepository,
        unit_of_work: AsyncUnitOfWork | None = None,
    ) -> None:
        """创建用户服务；旧独立仓储可暂不提供请求级 UoW。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    async def list(self) -> list[UserSnapshot]:
        """返回用户列表。"""
        return await self._repository.async_list()

    async def get_by_name(self, name: str) -> UserSnapshot | None:
        """按用户名查询用户。"""
        return await self._repository.async_get_by_name(name)

    async def get_by_id(self, user_id: int) -> UserSnapshot | None:
        """按用户 ID 查询用户。"""
        return await self._repository.async_get_by_id(user_id)

    async def create(self, payload: dict[str, Any]) -> UserSnapshot | None:
        """创建用户。"""
        return await self._write(lambda: self._repository.async_create(payload))

    async def update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> UserSnapshot | None:
        """更新用户。"""
        return await self._write(
            lambda: self._repository.async_update(user_id, payload)
        )

    async def delete(self, user_id: int) -> None:
        """删除用户。"""
        await self._write(lambda: self._repository.async_delete(user_id))

    async def update_otp(self, name: str, otp: bool, secret: str) -> None:
        """更新用户 OTP 状态。"""
        await self._write(
            lambda: self._repository.async_update_otp_by_name(name, otp, secret)
        )

    async def _write(self, operation: Callable[[], Awaitable[T]]) -> T:
        """执行用户写入，并在正式请求路径统一提交或回滚。"""
        try:
            result = await operation()
            if self._unit_of_work is not None:
                await self._unit_of_work.commit()
            return result
        except Exception:
            if self._unit_of_work is not None:
                await self._unit_of_work.rollback()
            raise


_configured_user_id_lookup: Callable[[int], UserSnapshot | None] | None = None
_configured_user_name_lookup: Callable[[str], UserSnapshot | None] | None = None
_configured_user_channel_lookup: Callable[..., str | None] | None = None


def configure_user_lookups(
    by_id: Callable[[int], UserSnapshot | None],
    by_name: Callable[[str], UserSnapshot | None],
    by_channel: Callable[..., str | None],
) -> None:
    """由启动组合根登记 ID、用户名和渠道身份查询能力。"""
    global _configured_user_id_lookup, _configured_user_name_lookup
    global _configured_user_channel_lookup
    _configured_user_id_lookup = by_id
    _configured_user_name_lookup = by_name
    _configured_user_channel_lookup = by_channel


def get_configured_user_id_lookup() -> Callable[[int], UserSnapshot | None]:
    """返回启动阶段登记的按 ID 用户查询函数。"""
    if _configured_user_id_lookup is None:
        raise RuntimeError("按 ID 的用户查询能力尚未配置")
    return _configured_user_id_lookup


def get_configured_user_name_lookup() -> Callable[[str], UserSnapshot | None]:
    """返回启动阶段登记的按用户名查询函数。"""
    if _configured_user_name_lookup is None:
        raise RuntimeError("按用户名的用户查询能力尚未配置")
    return _configured_user_name_lookup


def get_configured_user_channel_lookup() -> Callable[..., str | None]:
    """返回启动阶段登记的渠道身份到用户名查询函数。"""
    if _configured_user_channel_lookup is None:
        raise RuntimeError("渠道用户查询能力尚未配置")
    return _configured_user_channel_lookup
