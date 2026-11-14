"""
小组件登记表的显式会话读写；锁、事务提交和回滚均由调用方控制。
"""

from typing import cast

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.application.security.widget import WidgetRegistration as RegistrationSnapshot
from app.db.base import execute_dml
from app.db.models.widget import WidgetRegistration


class WidgetRegistrationOper:
    """
    仅查询或暂存小组件登记表，不创建会话或持有跨请求状态。
    """

    def __init__(self, session: Session) -> None:
        """
        绑定调用方独占的短事务会话。

        :param session: 已由组合根构造的同步 Session
        """
        self._session = session

    def get(self, registration_id: str) -> WidgetRegistration | None:
        """
        按外部随机标识读取记录，原始 secret 从未进入模型。

        :param registration_id: 服务端生成的规范 UUID
        :return: 当前事务内记录；不存在时为空
        """
        return cast(
            WidgetRegistration | None,
            self._session.scalar(
                select(WidgetRegistration).where(WidgetRegistration.registration_id == registration_id)
            ),
        )

    def replace(self, registration: RegistrationSnapshot, *, now: float, limit: int) -> bool:
        """
        在已持有用户写锁的事务内清理旧授权、检查名额并暂存安装替换。

        :param registration: 新身份与凭据摘要
        :param now: 过期清理的 UTC Unix 秒
        :param limit: 每用户允许的有效安装数
        :return: 成功暂存为真，新增安装超过上限为假；不提交事务
        """
        self._session.execute(
            delete(WidgetRegistration).where(
                WidgetRegistration.user_id == registration.user_id,
                or_(WidgetRegistration.expires_at <= now, WidgetRegistration.owner != registration.owner),
            )
        )
        existing = self._session.scalar(
            select(WidgetRegistration).where(
                WidgetRegistration.user_id == registration.user_id,
                WidgetRegistration.installation_id == registration.installation_id,
            )
        )
        count = self._session.scalar(
            select(func.count())
            .select_from(WidgetRegistration)
            .where(
                WidgetRegistration.user_id == registration.user_id,
            )
        )
        if existing is None and int(count or 0) >= limit:
            return False
        if existing is not None:
            self._session.delete(existing)
            # 先释放唯一安装约束，仍在同一个未提交事务内。
            self._session.flush()
        self._session.add(
            WidgetRegistration(
                registration_id=registration.registration_id,
                installation_id=registration.installation_id,
                user_id=registration.user_id,
                owner=registration.owner,
                secret_digest=registration.secret_digest,
                expires_at=registration.expires_at,
            )
        )
        self._session.flush()
        return True

    def delete(self, registration_id: str, owner: str) -> bool:
        """
        按随机标识和不可变 owner 一起删除，避免跨账号操作或探测。

        :param registration_id: 待撤销的外部 UUID
        :param owner: 已验证的本地账号不可变身份
        :return: 恰好删除一行时为真，不提交事务
        """
        return (
            execute_dml(
                self._session,
                delete(WidgetRegistration).where(
                    WidgetRegistration.registration_id == registration_id,
                    WidgetRegistration.owner == owner,
                ),
            )
            == 1
        )
