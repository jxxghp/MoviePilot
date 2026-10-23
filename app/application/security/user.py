"""
用户管理用例。

该模块承接用户端点需要的异步用户操作。具体数据库访问由请求组合根注入，
避免 API 层同时承担 HTTP 编排和 ORM 适配职责。
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional, Protocol, TypeAlias, TypeVar, Union, cast

from app.application.security.token import verify_password as _verify_password

FrozenJson: TypeAlias = Union[
    str,
    int,
    float,
    bool,
    None,
    tuple["FrozenJson", ...],
    Mapping[str, "FrozenJson"],
]
T = TypeVar("T")


def _freeze_json(value: Any) -> FrozenJson:
    """
    递归复制 JSON 值，阻止 ORM JSON 字段在会话外继续被修改。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return cast(FrozenJson, value)


def _freeze_mapping(
    value: Optional[Mapping[str, Any]],
) -> Mapping[str, FrozenJson]:
    """
    把可空 JSON 对象复制为只读映射。
    """
    frozen = _freeze_json(value or {})
    return cast(Mapping[str, FrozenJson], frozen)


@dataclass(frozen=True, slots=True)
class UserSnapshot:
    """
    脱离数据库会话的只读用户资料与权限快照。

    :param id: 数据库中的用户主键
    :param name: 本地用户名，也是用户配置的归属名称
    :param email: 用户邮箱；未设置时为 None
    :param is_active: 账号是否启用，禁用账号不能正常认证
    :param is_superuser: 是否拥有超级管理员权限
    :param avatar: 头像地址或内容；未设置时为 None
    :param is_otp: 是否启用基于时间的一次性密码验证
    :param permissions: 用户权限的只读快照
    :param settings: 用户个性化设置的只读快照
    :param notification_identity: 不可变的通知绑定身份，不从客户端设置中读取
    """

    id: int
    name: str
    email: Optional[str]
    is_active: bool
    is_superuser: bool
    avatar: Optional[str]
    is_otp: bool
    permissions: Mapping[str, FrozenJson]
    settings: Mapping[str, FrozenJson]
    notification_identity: str = ""

    @classmethod
    def build(
        cls,
        *,
        user_id: int,
        name: str,
        email: Optional[str],
        is_active: Optional[bool],
        is_superuser: Optional[bool],
        avatar: Optional[str],
        is_otp: Optional[bool],
        permissions: Optional[Mapping[str, Any]],
        settings: Optional[Mapping[str, Any]],
        notification_identity: str = "",
    ) -> "UserSnapshot":
        """
        复制持久化字段并构造不可变的公开用户快照。
        """
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
            notification_identity=notification_identity,
        )


@dataclass(frozen=True, slots=True)
class UserAuthSnapshot:
    """
    仅供认证链使用的只读用户凭据快照。

    :param user: 认证凭据对应的公开用户快照
    :param hashed_password: 用于密码校验的散列值；未设置时为 None
    :param otp_secret: 用于生成和核验一次性密码的共享密钥；未设置时为 None
    """

    user: UserSnapshot
    hashed_password: Optional[str]
    otp_secret: Optional[str]

    @property
    def id(self) -> int:
        """
        返回用户 ID。
        """
        return self.user.id

    @property
    def name(self) -> str:
        """
        返回用户名。
        """
        return self.user.name

    @property
    def is_active(self) -> bool:
        """
        返回账号启用状态。
        """
        return self.user.is_active

    @property
    def is_superuser(self) -> bool:
        """
        返回超级用户状态。
        """
        return self.user.is_superuser

    @property
    def avatar(self) -> Optional[str]:
        """
        返回用户头像。
        """
        return self.user.avatar

    @property
    def is_otp(self) -> bool:
        """
        返回 OTP 启用状态。
        """
        return self.user.is_otp

    @property
    def permissions(self) -> Mapping[str, FrozenJson]:
        """
        返回只读权限快照。
        """
        return self.user.permissions


@dataclass(frozen=True, slots=True)
class AuxiliaryUserCreate:
    """
    辅助认证首次落地本地用户所需的最小命令。

    :param name: 辅助认证通过后拟创建的本地用户名
    :param hashed_password: 待保存的密码散列，不接受明文密码
    :param is_active: 新账号是否启用，默认启用
    :param is_superuser: 是否创建为超级管理员，默认不授予
    """

    name: str
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = False


@dataclass(frozen=True, slots=True)
class UserUpdateResult:
    """
    用户更新事务产出的新快照与原用户名。

    :param user: 本次更新后的用户快照，由服务在事务提交后对外发布
    :param previous_name: 更新前的用户名，用于提交后迁移配置快照
    """

    user: UserSnapshot
    previous_name: str


class ChainUserRepository(Protocol):
    """
    用户 Chain 和 Agent 共享的类型化查询与创建端口。
    """

    def get_auth_by_name(self, name: str) -> Optional[UserAuthSnapshot]:
        """
        按用户名读取认证快照。

        :param name: 待查询或更新的本地用户名
        :return: 匹配的认证快照；不存在时返回 None
        """

    async def async_get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """
        异步按用户名读取公开用户快照。

        :param name: 待查询或更新的本地用户名
        :return: 匹配的公开用户快照；不存在时返回 None
        """

    async def async_get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """
        按已认证持久化 ID 读取公开用户快照，避免依赖旧会话展示名。

        :param user_id: 目标用户的数据库主键
        :return: 匹配的公开用户快照；不存在时返回 None
        """

    def create_auxiliary(self, command: AuxiliaryUserCreate) -> UserAuthSnapshot:
        """
        原子创建辅助认证用户并返回已提交快照。

        :param command: 已通过辅助认证的用户创建信息
        :return: 已成功提交的用户认证快照
        """

    def get_notification_settings(
        self,
        name: str,
    ) -> Optional[Mapping[str, FrozenJson]]:
        """
        读取通知路由设置；用户不存在时返回空值。

        :param name: 待查询或更新的本地用户名
        :return: 用户通知设置的只读快照；用户不存在时返回 None
        """

    async def async_get_notification_settings(
        self,
        name: str,
    ) -> Optional[Mapping[str, FrozenJson]]:
        """
        异步读取通知路由设置；用户不存在时返回空值。

        :param name: 待查询或更新的本地用户名
        :return: 用户通知设置的只读快照；用户不存在时返回 None
        """

    def find_name_by_bindings(
        self,
        bindings: Mapping[str, object],
        *,
        match_any: bool = False,
    ) -> Optional[str]:
        """
        默认匹配全部绑定；渠道候选字段可显式匹配任一项，仍须唯一启用用户。

        :param bindings: 渠道绑定字段到候选身份值的映射
        :param match_any: 是否接受任一绑定字段命中；默认要求全部匹配
        :return: 唯一匹配的启用用户名；无匹配或有歧义时返回 None
        """


class UserRepository(Protocol):
    """
    用户用例所需的最小异步数据端口。
    """

    async def async_has_users(self) -> bool:
        """
        判断数据库中是否已经存在任意用户。

        :return: 存在至少一个用户时返回 True
        """

    async def async_list(
        self,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> list[UserSnapshot]:
        """
        按可选窗口返回用户；两项均省略时返回全部。

        :param page: 页码，从 1 开始；与 count 均省略时不分页
        :param count: 每页记录数量；与 page 均省略时不分页
        :return: 按窗口读取的公开用户快照列表，无结果时为空列表
        """

    async def async_count(self) -> int:
        """
        返回用户总数。

        :return: 用户记录总数
        """

    async def async_get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """
        按用户名返回用户。

        :param name: 待查询或更新的本地用户名
        :return: 匹配的用户快照；不存在时返回 None
        """

    async def async_get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """
        按用户 ID 返回用户。

        :param user_id: 目标用户的数据库主键
        :return: 匹配的用户快照；不存在时返回 None
        """

    async def async_get_auth_by_id(self, user_id: int) -> Optional[UserAuthSnapshot]:
        """
        按用户 ID 返回密码校验所需的认证快照。

        :param user_id: 目标用户的数据库主键
        :return: 匹配的认证快照；不存在时返回 None
        """

    async def async_create(
        self,
        payload: dict[str, Any],
    ) -> Optional[UserSnapshot]:
        """
        创建用户并返回持久化对象。

        只暂存本次变更，事务提交或回滚由调用方工作单元负责。

        :param payload: 待创建或更新的用户字段，密码应由调用方预先散列
        :return: 暂存创建后的用户快照；未创建时返回 None，提交由调用方工作单元负责
        :raises UserNameConflictError: 待保存的用户名违反唯一约束
        """

    async def async_update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> Optional[UserUpdateResult]:
        """
        更新用户并返回提交后快照发布所需的变更结果。

        只暂存本次变更，事务提交或回滚由调用方工作单元负责。

        :param user_id: 目标用户的数据库主键
        :param payload: 待创建或更新的用户字段，密码应由调用方预先散列
        :return: 更新后的快照与原用户名；用户不存在时返回 None
        :raises UserNameConflictError: 待保存的用户名违反唯一约束
        :raises LastActiveSuperuserError: 变更会移除最后一个启用的超级管理员
        """

    async def async_delete(self, user_id: int) -> Optional[str]:
        """
        删除用户并返回被删除用户名。

        只暂存本次变更，事务提交或回滚由调用方工作单元负责。

        :param user_id: 目标用户的数据库主键
        :return: 已暂存删除的用户名；用户不存在时返回 None
        :raises LastActiveSuperuserError: 变更会移除最后一个启用的超级管理员
        """

    async def async_update_otp_by_name(self, name: str, otp: bool, secret: str) -> None:
        """
        更新用户 OTP 状态。

        只暂存本次变更，事务提交或回滚由调用方工作单元负责。

        :param name: 待查询或更新的本地用户名
        :param otp: 是否启用一次性密码认证
        :param secret: 待保存的一次性密码共享密钥
        """


class AsyncUnitOfWork(Protocol):
    """
    用户写用例所需的异步事务边界。
    """

    async def commit(self) -> None:
        """
        提交用户写入。
        """

    async def rollback(self) -> None:
        """
        回滚失败的用户写入。
        """


class UserConfigurationPublisher(Protocol):
    """
    用户聚合提交后同步进程级配置快照的应用端口。
    """

    async def rename(self, previous_name: str, current_name: str) -> None:
        """
        数据库改名提交后迁移对应用户名配置快照。

        :param previous_name: 事务提交前的用户名
        :param current_name: 事务提交后的新用户名
        """

    async def delete(self, username: str) -> None:
        """
        数据库删除提交后移除对应用户名配置快照。

        :param username: 配置所属的本地用户名
        """


class UserNameConflictError(Exception):
    """
    用户名在数据库唯一约束下发生冲突。
    """


class LastActiveSuperuserError(Exception):
    """
    用户变更会导致系统不再存在启用的超级管理员。
    """


class UserService:
    """
    用户管理应用服务。
    """

    def __init__(
        self,
        repository: UserRepository,
        unit_of_work: AsyncUnitOfWork,
        configuration: UserConfigurationPublisher,
    ) -> None:
        """
        创建用户服务并注入事务边界与提交后配置发布端口。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._configuration = configuration

    async def list(
        self,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> list[UserSnapshot]:
        """
        按可选数据库窗口返回用户列表。
        """
        if page is None and count is None:
            return await self._repository.async_list()
        return await self._repository.async_list(page=page, count=count)

    async def count(self) -> int:
        """
        返回用户精确总数。
        """
        return await self._repository.async_count()

    async def is_initialized(self) -> bool:
        """
        判断系统是否已经完成首次用户初始化。
        """
        return await self._repository.async_has_users()

    async def get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """
        按用户名查询用户。
        """
        return await self._repository.async_get_by_name(name)

    async def get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """
        按用户 ID 查询用户。
        """
        return await self._repository.async_get_by_id(user_id)

    async def verify_password(self, user_id: int, password: str) -> bool:
        """
        按用户 ID 读取认证快照并校验密码。
        """
        user = await self._repository.async_get_auth_by_id(user_id)
        return bool(
            user
            and user.user.is_active
            and user.hashed_password
            and _verify_password(password, user.hashed_password)
        )

    async def create(self, payload: dict[str, Any]) -> Optional[UserSnapshot]:
        """
        创建用户。
        """
        return await self._write(lambda: self._repository.async_create(payload))

    async def update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> Optional[UserSnapshot]:
        """
        更新用户。
        """
        result = await self._write(lambda: self._repository.async_update(user_id, payload))
        if result is None:
            return None
        if result.previous_name != result.user.name:
            await self._configuration.rename(result.previous_name, result.user.name)
        return result.user

    async def delete(self, user_id: int) -> None:
        """
        删除用户。
        """
        username = await self._write(lambda: self._repository.async_delete(user_id))
        if username is not None:
            await self._configuration.delete(username)

    async def update_otp(self, name: str, otp: bool, secret: str) -> None:
        """
        更新用户 OTP 状态。
        """
        await self._write(lambda: self._repository.async_update_otp_by_name(name, otp, secret))

    async def _write(self, operation: Callable[[], Awaitable[T]]) -> T:
        """
        执行用户写入，并在正式请求路径统一提交或回滚。
        """
        try:
            result = await operation()
            await self._unit_of_work.commit()
            return result
        except Exception:
            await self._unit_of_work.rollback()
            raise


_configured_user_id_lookup: Optional[Callable[[int], Optional[UserSnapshot]]] = None
_configured_user_name_lookup: Optional[Callable[[str], Optional[UserSnapshot]]] = None
_configured_user_channel_lookup: Optional[Callable[..., Optional[str]]] = None


def configure_user_lookups(
    by_id: Callable[[int], Optional[UserSnapshot]],
    by_name: Callable[[str], Optional[UserSnapshot]],
    by_channel: Callable[..., Optional[str]],
) -> None:
    """
    由启动组合根登记 ID、用户名和渠道身份查询能力。
    """
    global _configured_user_id_lookup, _configured_user_name_lookup
    global _configured_user_channel_lookup
    _configured_user_id_lookup = by_id
    _configured_user_name_lookup = by_name
    _configured_user_channel_lookup = by_channel


def reset_user_lookups() -> None:
    """
    清除当前应用生命周期 的全部用户身份查询函数。
    """
    global _configured_user_id_lookup, _configured_user_name_lookup
    global _configured_user_channel_lookup
    _configured_user_id_lookup = None
    _configured_user_name_lookup = None
    _configured_user_channel_lookup = None


def get_configured_user_id_lookup() -> Callable[[int], Optional[UserSnapshot]]:
    """
    返回启动阶段登记的按 ID 用户查询函数。
    """
    if _configured_user_id_lookup is None:
        raise RuntimeError("按 ID 的用户查询能力尚未配置")
    return _configured_user_id_lookup


def get_configured_user_name_lookup() -> Callable[[str], Optional[UserSnapshot]]:
    """
    返回启动阶段登记的按用户名查询函数。
    """
    if _configured_user_name_lookup is None:
        raise RuntimeError("按用户名的用户查询能力尚未配置")
    return _configured_user_name_lookup


def get_configured_user_channel_lookup() -> Callable[..., Optional[str]]:
    """
    返回启动阶段登记的渠道身份到用户名查询函数。
    """
    if _configured_user_channel_lookup is None:
        raise RuntimeError("渠道用户查询能力尚未配置")
    return _configured_user_channel_lookup
