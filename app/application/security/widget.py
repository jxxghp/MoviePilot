"""
小组件专用凭据的登记、轮换、撤销和当前权限校验，不接受主登录令牌。
"""

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias
from uuid import UUID, uuid4

from app.application.security.user import UserSnapshot
from app.schemas.widget import WidgetContent, WidgetContext, WidgetRegistrationResponse

# 前台主 App 通过已有登录更新；扩展不能无限自续。
WIDGET_CREDENTIAL_LIFETIME = 30 * 24 * 60 * 60
# 限制每个账号的安装授权数，轮换现有安装不占用额外名额。
WIDGET_REGISTRATION_LIMIT = 10
_OWNER = re.compile(r"[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"mpw_([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})\Z")


@dataclass(frozen=True, slots=True)
class WidgetRegistration:
    """
    脱离会话的最小凭据记录。

    :param registration_id: 每次轮换生成的新登记 UUID
    :param installation_id: 当前用户明确启用的本机安装 UUID
    :param user_id: 本地用户主键，仅用于重新读取当前账号
    :param owner: 不可变身份，阻止用户删除重建后继承旧授权
    :param secret_digest: 完整 Bearer 的 SHA-256 摘要，不保存原文
    :param expires_at: 有界凭据有效期的 UTC Unix 秒
    """

    registration_id: str
    installation_id: str
    user_id: int
    owner: str
    secret_digest: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True, slots=True)
class WidgetGrant:
    """
    一次快照读取的授权结果，不携带原始密钥或登录资料。

    :param registration: 已核验有效的登记记录
    :param user: 本次从数据库重新读取的用户及最新权限
    """

    registration: WidgetRegistration
    user: UserSnapshot


class WidgetUnitOfWork(Protocol):
    """
    短事务的用户、登记读写端口；调用方决定提交，退出未提交作用域必须回滚。
    """

    def get_user(self, user_id: int, *, lock: bool = False) -> UserSnapshot | None:
        """
        读取最新用户，写入操作先锁该用户以串行化名额检查及轮换。

        :param user_id: 本地用户主键
        :param lock: 是否持有跨数据库的排他事务锁直到作用域退出
        :return: 会话外用户快照；账号不存在时为空
        """
        ...

    def get_registration(self, registration_id: str) -> WidgetRegistration | None:
        """
        按随机登记标识读取摘要记录。

        :param registration_id: 规范 UUID
        :return: 不含原始密钥的登记；不存在时为空
        """
        ...

    def stage_registration(self, registration: WidgetRegistration, *, now: float, limit: int) -> bool:
        """
        暂存同用户同安装的原子替换，并删除该用户的过期及旧身份记录。

        :param registration: 服务端生成并绑定当前用户的新登记
        :param now: 过期清理的 UTC Unix 秒
        :param limit: 服务规定的每账号最大安装数
        :return: 成功暂存返回真；新增安装超过上限时返回假，不提交事务
        """
        ...

    def stage_delete(self, registration_id: str, owner: str) -> bool:
        """
        仅暂存指定 owner 的单项删除，不能通过随机 ID 操作他人登记。

        :param registration_id: 待撤销的规范 UUID
        :param owner: 已认证且不可变的用户身份
        :return: 匹配并删除时为真；不暴露其他账号的记录存在性
        """
        ...

    def commit(self) -> None:
        """
        提交本次短事务；提交失败向上传播，不返回未落地的凭据。
        """
        ...


WidgetScopeFactory: TypeAlias = Callable[[], AbstractContextManager[WidgetUnitOfWork]]


class WidgetAccessError(ValueError):
    """
    供 HTTP 边界翻译的固定鉴权失败；不得携带原始凭据或数据库异常。
    """

    def __init__(self, status: int, message: str) -> None:
        """
        保存允许公开的状态与中文原因。

        :param status: HTTP 边界使用的 401、403、404 或 409
        :param message: 不含敏感数据的固定说明
        """
        super().__init__(message)
        self.status = status


def available_widget_contents(user: UserSnapshot) -> list[WidgetContent]:
    """
    复用原网页分类和功能权限的判定语义，缺省功能继承分类权限。

    :param user: 数据库的当前激活用户快照，忽略普通用户可编辑的 admin 字段
    :return: 固定顺序的可用内容；管理员放行全部
    """
    contents: list[WidgetContent] = []
    if user.is_superuser:
        contents.append("overview")
    features = user.permissions.get("features")
    features = features if isinstance(features, Mapping) else {}
    permissions: tuple[tuple[WidgetContent, str, str], ...] = (
        ("downloads", "manage", "manage.downloading"),
        ("imports", "manage", "manage.history"),
    )
    for content, category, feature in permissions:
        if user.is_superuser or (user.permissions.get(category) is True and features.get(feature) is not False):
            # 元组来自固定服务端枚举，避免让客户端指定权限名称。
            contents.append(content)
    if widget_subscription_features(user):
        contents.append("subscriptions")
    return contents


def widget_subscription_features(user: UserSnapshot) -> tuple[str, ...]:
    """
    合并订阅组件仍逐类复核权限，禁止把被禁用类型算入总数或摘要。

    :param user: 当前激活用户的不可变授权快照
    :return: 当前可用的固定订阅功能标识
    """
    features = user.permissions.get("features")
    features = features if isinstance(features, Mapping) else {}
    return tuple(
        feature for feature in ("subscribe.movie", "subscribe.tv", "subscribe.music")
        if user.is_superuser or (user.permissions.get("subscribe") is True and features.get(feature) is not False)
    )


class WidgetAuthService:
    """
    通过注入的独占短事务管理专用只读授权，不依赖进程缓存或通用 API_TOKEN。
    """

    def __init__(self, scope: WidgetScopeFactory, *, clock: Callable[[], float] = time.time) -> None:
        """
        注入存储作用域与时钟，便于离线验证撤销、到期和并发事务。

        :param scope: 每次调用构造独占会话，不能跨线程复用
        :param clock: 可信 UTC Unix 秒来源
        """
        self._scope = scope
        self._clock = clock

    @staticmethod
    def _require_user(user: UserSnapshot | None, owner: str) -> UserSnapshot:
        """
        禁用、删除重建及缺失不可变身份均拒绝继承旧授权。
        """
        if user is None or not user.is_active or not _OWNER.fullmatch(owner) or user.notification_identity != owner:
            raise WidgetAccessError(401, "小组件授权已失效，请在 App 中重新启用")
        return user

    def context(self, user_id: int, owner: str) -> WidgetContext:
        """
        重新读取主登录账号的不可变身份和权限，不创建或轮换小组件授权。

        :param user_id: 既有主认证给出的用户主键
        :param owner: 既有主认证给出的不可变身份，不接受请求参数指定
        :return: 当前身份和可用内容，无可用权限时返回空集合
        :raises WidgetAccessError: 账号禁用、删除或身份已变化
        """
        with self._scope() as transaction:
            user = self._require_user(transaction.get_user(user_id), owner)
            return WidgetContext(owner=user.notification_identity, availableContents=available_widget_contents(user))

    def register(self, user_id: int, owner: str, installation_id: UUID) -> WidgetRegistrationResponse:
        """
        原子轮换本安装凭据，只有成功提交后的新密钥可以返回客户端。

        同安装并发登记按提交顺序覆盖；响应丢失后重试会再次轮换，旧响应不能续用。
        客户端只保留最后完成的串行登记，不能并发提交或自动重放。

        :param user_id: 既有认证给出的用户主键
        :param owner: 既有认证给出的不可变身份，不接受请求体指定
        :param installation_id: 本机安装的随机标识
        :return: 已提交且只返回一次的专用凭据
        """
        now = self._clock()
        with self._scope() as transaction:
            user = self._require_user(transaction.get_user(user_id, lock=True), owner)
            contents = available_widget_contents(user)
            if not contents:
                raise WidgetAccessError(403, "当前账号没有可用于小组件的内容")
            registration_id = uuid4()
            token = f"mpw_{registration_id}.{secrets.token_urlsafe(32)}"
            registration = WidgetRegistration(
                registration_id=str(registration_id),
                installation_id=str(installation_id),
                user_id=user.id,
                owner=owner,
                secret_digest=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + WIDGET_CREDENTIAL_LIFETIME,
            )
            if not transaction.stage_registration(registration, now=now, limit=WIDGET_REGISTRATION_LIMIT):
                raise WidgetAccessError(409, "小组件安装数已达上限，请先移除一个已启用的安装")
            transaction.commit()
        return WidgetRegistrationResponse(
            registrationId=registration_id,
            installationId=installation_id,
            owner=owner,
            secret=token,
            expiresAt=registration.expires_at,
            availableContents=contents,
        )

    def _grant(self, transaction: WidgetUnitOfWork, registration: WidgetRegistration | None) -> WidgetGrant:
        """
        每次核验当前用户、不可变身份、有效期与最新权限，不允许自续期。
        """
        if registration is None or registration.expires_at <= self._clock():
            raise WidgetAccessError(401, "小组件授权已失效，请在 App 中重新启用")
        user = self._require_user(transaction.get_user(registration.user_id), registration.owner)
        if not available_widget_contents(user):
            raise WidgetAccessError(403, "当前账号没有可用于小组件的内容")
        return WidgetGrant(registration, user)

    def authenticate(self, token: str) -> WidgetGrant:
        """
        只接受固定格式的专用 Bearer；主 JWT、通用 API_TOKEN 和资源令牌不能互换。

        :param token: 从专用 HTTP Header 依赖取得的凭据，不接受 URL 或 Cookie
        :return: 不携带原文凭据的当前授权
        """
        match = _TOKEN.fullmatch(token)
        if match is None:
            raise WidgetAccessError(401, "需要有效的小组件专用 Bearer")
        try:
            registration_id = str(UUID(match[1]))
        except ValueError as error:
            raise WidgetAccessError(401, "需要有效的小组件专用 Bearer") from error
        with self._scope() as transaction:
            registration = transaction.get_registration(registration_id)
            digest = hashlib.sha256(token.encode()).hexdigest()
            if registration is None or not hmac.compare_digest(digest, registration.secret_digest):
                raise WidgetAccessError(401, "小组件授权已失效，请在 App 中重新启用")
            return self._grant(transaction, registration)

    def revalidate(self, grant: WidgetGrant) -> None:
        """
        读取外部来源后再次确认撤销与权限，阻止等待中的旧授权返回数据。

        :param grant: 本次请求开始时的授权，摘要不会离开服务端
        """
        with self._scope() as transaction:
            current = self._grant(transaction, transaction.get_registration(grant.registration.registration_id))
            if current.registration != grant.registration or current.user != grant.user:
                raise WidgetAccessError(403, "小组件账号或权限已变化，请重新获取")

    def revoke(self, user_id: int, owner: str, registration_id: UUID) -> None:
        """
        主登录只撤销本人指定登记，不能枚举或管理他人安装。

        :param user_id: 既有认证给出的用户主键
        :param owner: 既有认证给出的不可变身份
        :param registration_id: 待撤销的随机登记标识
        """
        with self._scope() as transaction:
            self._require_user(transaction.get_user(user_id, lock=True), owner)
            if not transaction.stage_delete(str(registration_id), owner):
                raise WidgetAccessError(404, "小组件登记不存在")
            transaction.commit()

    def revoke_self(self, grant: WidgetGrant) -> None:
        """
        专用凭据只能删除自身，不能指定其它登记，也不能借此延长有效期。

        :param grant: 当前专用 Bearer 对应的授权
        """
        self.revoke(grant.user.id, grant.registration.owner, UUID(grant.registration.registration_id))
