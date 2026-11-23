"""
媒体订阅旧记录的人工归属确认。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol

from app.application.subscription.contract import SubscriptionHistorySnapshot, SubscriptionSnapshot
from app.application.subscription.delete import AsyncUnitOfWork
from app.runtime.log import logger

type SubscriptionRecordKind = Literal["active", "history"]
type SubscriptionOwnerResult = Literal["confirmed", "changed", "invalid_account"]


@dataclass(frozen=True, slots=True)
class SubscriptionOwnerAccount:
    """
    可认领订阅的有效本地账号及其复核版本。

    :param id: 管理员选择的有效本地账号主键
    :param name: 复核时显示的账号名称，不作为不可变归属依据
    :param revision: 由当前账号身份及名称生成的并发复核版本
    """

    id: int
    name: str
    revision: str


def _review_revision(facts: list[object]) -> str:
    """
    从复核时可见的持久化事实计算版本。
    """
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()


def account_review_revision(account_id: int, name: str, identity: str) -> str:
    """
    防止过期页面确认已被删除重建或更名的账号。
    """
    return _review_revision([account_id, name, identity])


def subscription_owner_revision(record: SubscriptionSnapshot | SubscriptionHistorySnapshot) -> str:
    """
    绑定管理员看到的旧记录，防止迟到的确认关联另一条记录。
    """
    facts: list[object] = [record.id, record.activity_key, record.name, record.username, record.date,
             record.type, record.media_source, record.media_id, record.season, record.music_type, record.episode_group]
    return _review_revision(facts)


class SubscriptionOwnerRepository(Protocol):
    """
    在同一事务里核验目标账号并暂存人工归属。
    """

    async def list_accounts(self) -> list[SubscriptionOwnerAccount]:
        """
        列出可选的有效本地账号。
        """
        ...

    async def stage_owner(
        self, kind: SubscriptionRecordKind, record_id: int, revision: str,
        account_id: int, account_revision: str,
    ) -> SubscriptionOwnerResult:
        """
        仅确认尚无归属且仍匹配复核版本的记录。
        """
        ...


class ConfirmSubscriptionOwner:
    """
    统一提交旧订阅和订阅历史的人工确认结果。
    """

    def __init__(self, repository: SubscriptionOwnerRepository, unit_of_work: AsyncUnitOfWork) -> None:
        """
        :param repository: 当前请求事务使用的归属仓储
        :param unit_of_work: 当前请求的事务边界
        """
        self._repository = repository
        self._unit_of_work = unit_of_work

    async def list_accounts(self) -> list[SubscriptionOwnerAccount]:
        """
        查询当前可选的目标账号。
        """
        return await self._repository.list_accounts()

    async def execute(
        self, kind: SubscriptionRecordKind, record_id: int, revision: str, account_id: int,
        account_revision: str, administrator_id: int,
    ) -> SubscriptionOwnerResult:
        """
        保存管理员已审核的目标账号，对并发修改和归属冲突回滚。
        """
        try:
            result = await self._repository.stage_owner(kind, record_id, revision, account_id, account_revision)
            if result != "confirmed":
                await self._unit_of_work.rollback()
                return result
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        logger.info("媒体订阅归属已确认：记录=%s/%s，管理员=%s，目标账号=%s",
                    kind, record_id, administrator_id, account_id)
        return result
