"""
浏览器推送登记、账号目标选择与版本化撤销的应用服务及持久化端口。
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from hmac import compare_digest
from typing import Any, Optional, Protocol
from uuid import uuid4

from app.application.security.user import UserSnapshot
from app.schemas.webpush import WebPushSubscription


class WebPushConflictError(ValueError):
    """
    端点归属证明不匹配或登记版本已变化，调用方应重新读取浏览器订阅。
    """


@dataclass(frozen=True, slots=True)
class WebPushRegistration:
    """
    已归属账号的不可变订阅快照，不向客户端暴露服务端 owner。

    :param endpoint: 浏览器推送地址
    :param p256dh: 浏览器的推送公钥
    :param auth: 浏览器持有的认证密钥
    :param user_id: 用于外键清理的账号主键，不能单独决定投递归属
    :param owner: 数据库分配的不可变通知身份
    :param revision: 每次登记生成的新版本，阻止旧失败删除新订阅
    """

    endpoint: str
    p256dh: str = field(repr=False)
    auth: str = field(repr=False)
    user_id: int
    owner: str
    revision: str

    def subscription(self) -> dict[str, Any]:
        """
        仅返回推送协议字段，避免向远端泄漏账号与版本。

        :return: 可交给 Web Push 发送器的订阅数据
        """
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class WebPushUnitOfWork(Protocol):
    """
    一次短事务的账号核验、订阅暂存和提交端口。
    """

    def get_user(self, user_id: int) -> Optional[UserSnapshot]:
        """
        锁定并读取最新账号，直到事务结束。

        :param user_id: 服务端认证得到的账号主键
        :return: 当前账号快照；已删除时为空
        """
        ...

    def get(self, endpoint: str) -> Optional[WebPushRegistration]:
        """
        读取已知端点的冻结快照。

        :param endpoint: 浏览器订阅端点
        :return: 当前登记或空
        """
        ...

    def replace(self, registration: WebPushRegistration, previous: Optional[WebPushRegistration]) -> bool:
        """
        按旧版本条件暂存替换，唯一端点冲突不能覆盖并发结果。

        :param registration: 新登记
        :param previous: 用于比较并交换的旧快照；新增时为空
        :return: 成功暂存为真；版本变化为假
        """
        ...

    def list_active(self, owners: Optional[tuple[str, ...]]) -> tuple[WebPushRegistration, ...]:
        """
        只读取仍属启用本地账号的订阅，显式空集合不能解释成全体。

        :param owners: 不可变账号目标；仅管理全体路径传入 None
        :return: 可投递登记快照
        """
        ...

    def delete(self, registration: WebPushRegistration) -> bool:
        """
        按端点、owner 和版本同时删除，阻止迟到的失败或撤销串号。

        :param registration: 要撤销的确切快照
        :return: 删除当前版本时为真
        """
        ...

    def commit(self) -> None:
        """
        提交当前独占事务，失败直接向上传播。
        """
        ...


class WebPushService:
    """
    由登录主体登记设备，个人选择和管理全体选择使用独立入口。
    """

    def __init__(self, scope: Callable[[], AbstractContextManager[WebPushUnitOfWork]]) -> None:
        """
        保存启动组合根注入的短事务工厂。

        :param scope: 每次调用创建一个独占事务
        """
        self._scope = scope

    @staticmethod
    def _owner(transaction: WebPushUnitOfWork, user_id: int) -> str:
        """
        从数据库当前启用的本地账号读取 owner，拒绝内部渠道账号。

        :param transaction: 当前独占事务
        :param user_id: 当前登录主体主键
        :return: 不可变通知身份
        :raises PermissionError: 账号删除、停用或不是本地账号
        """
        user = transaction.get_user(user_id)
        if not user or not user.is_active or user.account_type != "local" or not user.notification_identity:
            raise PermissionError("请使用启用的本地账号重新登录")
        return user.notification_identity

    def register(self, user_id: int, subscription: WebPushSubscription) -> None:
        """
        当前登录证明账号身份，完整端点和原密钥证明跨账号换绑时的订阅持有权。

        同一账号允许更新密钥；跨账号必须与现存两个密钥完全一致，否则返回冲突。
        旧无归属内存登记不作为持有证明或新订阅事实源。

        :param user_id: 可信认证依赖注入的账号主键
        :param subscription: 浏览器当前持有的完整订阅，不包含 owner
        :raises WebPushConflictError: 换绑证明不匹配或并发版本发生变化
        """
        with self._scope() as transaction:
            owner = self._owner(transaction, user_id)
            previous = transaction.get(subscription.endpoint)
            if previous and previous.owner != owner and not (
                compare_digest(previous.p256dh, subscription.keys.p256dh)
                and compare_digest(previous.auth, subscription.keys.auth)
            ):
                raise WebPushConflictError("订阅持有证明不匹配，请取消浏览器订阅后重新登记")
            registration = WebPushRegistration(
                endpoint=subscription.endpoint,
                p256dh=subscription.keys.p256dh,
                auth=subscription.keys.auth,
                user_id=user_id,
                owner=owner,
                revision=uuid4().hex,
            )
            if not transaction.replace(registration, previous):
                raise WebPushConflictError("订阅已被更新，请重新登记")
            transaction.commit()

    def unregister(self, user_id: int, subscription: WebPushSubscription) -> None:
        """
        仅撤销本人且密钥仍匹配的订阅，跨账号或迟到请求保持幂等无副作用。

        :param user_id: 当前登录主体的可信主键
        :param subscription: 待撤销的浏览器订阅
        """
        with self._scope() as transaction:
            owner = self._owner(transaction, user_id)
            current = transaction.get(subscription.endpoint)
            if current and current.owner == owner and (
                compare_digest(current.p256dh, subscription.keys.p256dh)
                and compare_digest(current.auth, subscription.keys.auth)
            ):
                transaction.delete(current)
            transaction.commit()

    def for_owners(self, owners: tuple[str, ...]) -> tuple[WebPushRegistration, ...]:
        """
        按通知链明确提供的不可变账号身份选择当前有效设备。

        :param owners: 个人或显式全体路由已经确定的账号集合
        :return: 不含旧无归属订阅的快照
        """
        if not owners:
            return ()
        with self._scope() as transaction:
            return transaction.list_active(owners)

    def management_targets(self) -> tuple[WebPushRegistration, ...]:
        """
        仅供超级管理员发送端点读取当前有效账号的全部设备。

        :return: 已持久化且归属有效的登记快照
        """
        with self._scope() as transaction:
            return transaction.list_active(None)

    def is_current(self, registration: WebPushRegistration) -> bool:
        """
        发送前确认账号仍启用，且端点仍属于计划中的登记版本。

        :param registration: 派发前冻结的设备登记
        :return: 快照和账号均有效时为真
        """
        with self._scope() as transaction:
            try:
                owner = self._owner(transaction, registration.user_id)
            except PermissionError:
                return False
            return owner == registration.owner and transaction.get(registration.endpoint) == registration

    def remove_expired(self, registration: WebPushRegistration) -> bool:
        """
        仅清理本次发送使用的 owner 和版本，不删除已换绑或重新登记的设备。

        :param registration: 收到 404 或 410 的投递快照
        :return: 当前版本确实删除时为真
        """
        with self._scope() as transaction:
            removed = transaction.delete(registration)
            transaction.commit()
            return removed


_service: Optional[WebPushService] = None


def configure_webpush_service(service: Optional[WebPushService]) -> None:
    """
    由启动组合根发布或撤销浏览器推送服务。

    :param service: 新服务；关停时传入 None
    """
    global _service
    _service = service


def get_webpush_service() -> WebPushService:
    """
    返回已装配服务，缺少装配时拒绝回退到无归属内存登记表。

    :return: 当前启动周期的服务
    :raises RuntimeError: 服务尚未装配或已关停
    """
    if _service is None:
        raise RuntimeError("浏览器推送服务未初始化")
    return _service
