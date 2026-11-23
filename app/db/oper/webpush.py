"""
浏览器订阅的表级查询及按版本条件暂存，不拥有事务提交权。
"""

from hashlib import sha256
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.application.webpush import WebPushRegistration
from app.db.base import execute_dml
from app.db.models.user import User
from app.db.models.webpush import WebPushSubscription


class WebPushSubscriptionOper:
    """
    使用调用方会话操作唯一端点和有效账号集合。
    """

    def __init__(self, session: Session) -> None:
        """
        保存当前短事务会话。

        :param session: 调用方持有的同步会话
        """
        self._session = session

    @staticmethod
    def _digest(endpoint: str) -> str:
        """
        生成完整端点的稳定摘要，不把原始地址写入索引或日志。

        :param endpoint: 完整推送端点
        :return: SHA-256 十六进制摘要
        """
        return sha256(endpoint.encode()).hexdigest()

    @staticmethod
    def _snapshot(row: WebPushSubscription) -> WebPushRegistration:
        """
        在会话内复制全部投递和比较交换字段。

        :param row: ORM 登记记录
        :return: 不依赖数据库会话的冻结快照
        """
        return WebPushRegistration(
            endpoint=row.endpoint, p256dh=row.p256dh, auth=row.auth,
            user_id=row.user_id, owner=row.owner, revision=row.revision,
        )

    def get(self, endpoint: str) -> Optional[WebPushRegistration]:
        """
        按完整端点读取现有登记，摘要碰撞不能替代身份比较。

        :param endpoint: 原始完整推送地址
        :return: 登记快照或空
        """
        row = self._session.scalar(select(WebPushSubscription).where(
            WebPushSubscription.endpoint_digest == self._digest(endpoint)
        ))
        return self._snapshot(row) if row and row.endpoint == endpoint else None

    def replace(self, registration: WebPushRegistration, previous: Optional[WebPushRegistration]) -> bool:
        """
        新增或比较交换当前版本，冲突由适配器翻译且不吞并发更新。

        :param registration: 新登记快照
        :param previous: 应用已验证的前一版本
        :return: 暂存成功为真，旧版本变化为假
        """
        values = {
            "endpoint": registration.endpoint, "p256dh": registration.p256dh,
            "auth": registration.auth, "user_id": registration.user_id,
            "owner": registration.owner, "revision": registration.revision,
        }
        digest = self._digest(registration.endpoint)
        if previous is None:
            self._session.add(WebPushSubscription(endpoint_digest=digest, **values))
            self._session.flush()
            return True
        return execute_dml(
            self._session,
            update(WebPushSubscription).where(
                WebPushSubscription.endpoint_digest == digest,
                WebPushSubscription.endpoint == previous.endpoint,
                WebPushSubscription.owner == previous.owner,
                WebPushSubscription.revision == previous.revision,
            ).values(**values),
        ) == 1

    def list_active(self, owners: Optional[tuple[str, ...]]) -> tuple[WebPushRegistration, ...]:
        """
        重新关联启用本地账号及不可变身份，避免停用、删除重建和主键复用。

        :param owners: 明确账号集合；仅管理全体入口传 None
        :return: 当前仍可投递的快照
        """
        statement = select(WebPushSubscription).join(User, User.id == WebPushSubscription.user_id).where(
            User.is_active.is_(True), User.account_type == "local",
            User.notification_identity == WebPushSubscription.owner,
        )
        if owners is not None:
            statement = statement.where(WebPushSubscription.owner.in_(owners))
        return tuple(self._snapshot(row) for row in self._session.scalars(
            statement.order_by(WebPushSubscription.endpoint_digest)
        ))

    def delete(self, registration: WebPushRegistration) -> bool:
        """
        只删除投递使用的同端点、同账号、同版本快照。

        :param registration: 当前操作读到的完整快照
        :return: 删除一行时为真，不提交事务
        """
        return execute_dml(self._session, delete(WebPushSubscription).where(
            WebPushSubscription.endpoint_digest == self._digest(registration.endpoint),
            WebPushSubscription.endpoint == registration.endpoint,
            WebPushSubscription.owner == registration.owner,
            WebPushSubscription.revision == registration.revision,
        )) == 1
