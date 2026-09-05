"""订阅写入端口的 SQLAlchemy 事务适配器。"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Optional, TypeVar, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.outbox import (
    OUTBOX_LEASE_SECONDS,
    AsyncOutboxDispatchStore,
    ClaimedOutboxMessage,
    OutboxDispatchStore,
    OutboxLeaseLostError,
)
from app.application.subscription.contract import (
    AfterCommitEffect as TypedAfterCommitEffect,
)
from app.application.subscription.contract import (
    AsyncAfterCommitEffect as TypedAsyncAfterCommitEffect,
)
from app.application.subscription.contract import (
    SubscribeDeletionCandidate,
    SubscriptionHistoryPatch,
    SubscriptionHistorySnapshot,
    SubscriptionIdentity,
    SubscriptionPatch,
    SubscriptionSnapshot,
    SubscriptionStagingPort,
    SubscriptionWriteResult,
    subscription_added_event_key,
    subscription_added_notification_key,
    subscription_added_report_key,
)
from app.application.subscription.write import (
    AsyncCreateSubscriptionBatchCommand,
    AsyncCreateSubscriptionCommand,
    AsyncSubscriptionOutboxStager,
    AsyncUnitOfWork,
    CreateSubscriptionCommand,
    SubscriptionCreateRequest,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.common import JsonData
from app.schemas.types import MediaSource

T = TypeVar("T")


def _media_source(value: Optional[str]) -> Optional[MediaSource]:
    """把持久化字符串恢复为稳定媒体来源枚举。"""
    return MediaSource(value) if value else None


def _project_subscription(record: Subscribe) -> SubscriptionSnapshot:
    """在 ORM 所属 Session 内复制完整订阅记录。"""
    return SubscriptionSnapshot(
        id=record.id,
        name=record.name,
        year=record.year,
        type=record.type,
        keyword=record.keyword,
        media_source=_media_source(record.media_source),
        media_id=record.media_id,
        music_type=record.music_type,
        total_tracks=record.total_tracks,
        season=record.season,
        poster=record.poster,
        backdrop=record.backdrop,
        vote=record.vote,
        description=record.description,
        filter=record.filter,
        include=record.include,
        exclude=record.exclude,
        quality=record.quality,
        resolution=record.resolution,
        effect=record.effect,
        audio_quality=record.audio_quality,
        audio_format=record.audio_format,
        min_bitrate=record.min_bitrate,
        min_bit_depth=record.min_bit_depth,
        min_sample_rate=record.min_sample_rate,
        total_episode=record.total_episode,
        start_episode=record.start_episode,
        lack_episode=record.lack_episode,
        note=cast(Optional[builtins.list[int]], record.note),
        state=record.state,
        last_update=record.last_update,
        date=record.date,
        username=record.username,
        sites=cast(Optional[builtins.list[int]], record.sites),
        downloader=record.downloader,
        best_version=record.best_version,
        best_version_full=record.best_version_full,
        current_priority=record.current_priority,
        current_audio_format=record.current_audio_format,
        current_bitrate=record.current_bitrate,
        current_bit_depth=record.current_bit_depth,
        current_sample_rate=record.current_sample_rate,
        episode_priority=cast(Optional[dict[str, int]], record.episode_priority),
        save_path=record.save_path,
        search_imdbid=record.search_imdbid,
        manual_total_episode=record.manual_total_episode,
        custom_words=record.custom_words,
        media_category_id=record.media_category_id,
        media_category=record.media_category,
        filter_groups=cast(Optional[builtins.list[str]], record.filter_groups),
        episode_group=record.episode_group,
    )


def _project_history(record: SubscribeHistory) -> SubscriptionHistorySnapshot:
    """在 ORM 所属 Session 内复制完整订阅历史记录。"""
    return SubscriptionHistorySnapshot(
        id=record.id,
        name=record.name,
        year=record.year,
        type=record.type,
        keyword=record.keyword,
        media_source=_media_source(record.media_source),
        media_id=record.media_id,
        music_type=record.music_type,
        total_tracks=record.total_tracks,
        season=record.season,
        poster=record.poster,
        backdrop=record.backdrop,
        vote=record.vote,
        description=record.description,
        filter=record.filter,
        include=record.include,
        exclude=record.exclude,
        quality=record.quality,
        resolution=record.resolution,
        effect=record.effect,
        audio_quality=record.audio_quality,
        audio_format=record.audio_format,
        min_bitrate=record.min_bitrate,
        min_bit_depth=record.min_bit_depth,
        min_sample_rate=record.min_sample_rate,
        total_episode=record.total_episode,
        start_episode=record.start_episode,
        date=record.date,
        username=record.username,
        sites=cast(Optional[builtins.list[int]], record.sites),
        best_version=record.best_version,
        best_version_full=record.best_version_full,
        current_priority=record.current_priority,
        current_audio_format=record.current_audio_format,
        current_bitrate=record.current_bitrate,
        current_bit_depth=record.current_bit_depth,
        current_sample_rate=record.current_sample_rate,
        episode_priority=cast(Optional[dict[str, int]], record.episode_priority),
        save_path=record.save_path,
        search_imdbid=record.search_imdbid,
        custom_words=record.custom_words,
        media_category_id=record.media_category_id,
        media_category=record.media_category,
        classification_rule_id=record.classification_rule_id,
        classification_policy_revision=record.classification_policy_revision,
        classification_source=record.classification_source,
        filter_groups=cast(Optional[builtins.list[str]], record.filter_groups),
        episode_group=record.episode_group,
    )


class _TransactionalSubscriptionWriter:
    """封装独立 Session 内的订阅新增与 durable intent 结算。"""

    def __init__(
        self,
        sync_session: Callable[[], Session],
        async_session: Callable[
            [],
            AbstractAsyncContextManager[AsyncSession],
        ],
    ) -> None:
        """注入同步会话工厂和异步会话作用域。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: str | None = None,
        after_commit: TypedAfterCommitEffect | None = None,
        notification: Mapping[str, JsonData] | None = None,
        occurrence_id: str | None = None,
    ) -> tuple[int, str]:
        """在独占同步会话内执行一次完整订阅新增事务。"""
        resolved_occurrence_id = occurrence_id or uuid4().hex
        write_payload = payload.to_payload()
        notification_payload = dict(notification) if notification else None
        session = self._sync_session()
        try:
            outbox = SqlAlchemyOutboxStager(session)
            dispatch_store = SqlAlchemyOutboxDispatchStore(self._sync_session)
            command = CreateSubscriptionCommand(
                repository=SessionSubscriptionRepository(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )

            def delivered(subscribe_id: int) -> None:
                """执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    _deliver_added_effects(
                        dispatch_store,
                        subscribe_id,
                        write_payload,
                        notification_payload,
                        lambda: after_commit(subscribe_id),
                        occurrence_id=resolved_occurrence_id,
                    )

            return command.execute(
                identity,
                payload,
                username,
                delivered,
                notification_payload,
                occurrence_id=resolved_occurrence_id,
            )
        finally:
            session.close()

    async def async_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: str | None = None,
        after_commit: TypedAsyncAfterCommitEffect | None = None,
        notification: Mapping[str, JsonData] | None = None,
        occurrence_id: str | None = None,
    ) -> tuple[int, str]:
        """在独占异步会话作用域内执行一次完整订阅新增事务。"""
        resolved_occurrence_id = occurrence_id or uuid4().hex
        write_payload = payload.to_payload()
        notification_payload = dict(notification) if notification else None
        async with self._async_session() as session:
            outbox = SqlAlchemyAsyncOutboxStager(session)
            dispatch_store = SqlAlchemyAsyncOutboxDispatchStore(self._async_session)
            command = AsyncCreateSubscriptionCommand(
                repository=SessionSubscriptionRepository(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                outbox=outbox,
            )

            async def delivered(subscribe_id: int) -> None:
                """异步执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    await _deliver_added_effects_async(
                        dispatch_store,
                        subscribe_id,
                        write_payload,
                        notification_payload,
                        lambda: after_commit(subscribe_id),
                        occurrence_id=resolved_occurrence_id,
                    )

            return await command.execute(
                identity,
                payload,
                username,
                delivered,
                notification_payload,
                occurrence_id=resolved_occurrence_id,
            )


class TransactionalSubscriptionRepository(_TransactionalSubscriptionWriter):
    """以独立短 Session 实现订阅查询、新增和历史查询端口。"""

    def _read(self, operation: Callable[[SubscribeOper], T]) -> T:
        """在独立同步 Session 中执行查询并投影快照。"""
        with self._sync_session() as session:
            return operation(SubscribeOper(session))

    async def _async_read(
        self,
        operation: Callable[[SubscribeOper], Awaitable[T]],
    ) -> T:
        """在独立异步 Session 中执行查询并投影快照。"""
        async with self._async_session() as session:
            return await operation(SubscribeOper(session))

    def exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断媒体身份是否已有订阅。"""
        return bool(
            self._read(
                lambda repository: repository.exists(
                    identity.media_source,
                    identity.media_id,
                    identity.season,
                    identity.episode_group,
                    identity.music_type,
                )
            )
        )

    def history_exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断媒体身份是否已有订阅历史。"""
        with self._sync_session() as session:
            return SubscribeHistoryOper(session).exists(
                identity.media_source,
                identity.media_id,
                identity.season,
                identity.episode_group,
                identity.music_type,
            )

    def get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """同步按主键读取订阅快照。"""
        return self._read(
            lambda repository: (
                _project_subscription(record) if (record := repository.get(subscribe_id)) is not None else None
            )
        )

    def get_by(self, identity: SubscriptionIdentity) -> Optional[SubscriptionSnapshot]:
        """同步按来源身份读取订阅快照。"""
        if identity.type is None:
            return None
        return self._read(
            lambda repository: (
                _project_subscription(record)
                if (
                    record := repository.get_by(
                        type=cast(str, identity.type),
                        media_source=identity.media_source,
                        media_id=identity.media_id,
                        season=identity.season,
                        music_type=identity.music_type,
                    )
                )
                is not None
                else None
            )
        )

    def list(self, state: Optional[str] = None) -> builtins.list[SubscriptionSnapshot]:
        """同步按可选状态读取订阅快照。"""
        return self._read(lambda repository: [_project_subscription(record) for record in repository.list(state)])

    async def async_get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """异步按主键读取订阅快照。"""

        async def operation(repository: SubscribeOper) -> Optional[SubscriptionSnapshot]:
            """读取并在当前 Session 中投影订阅。"""
            record = await repository.async_get(subscribe_id)
            return _project_subscription(record) if record is not None else None

        return await self._async_read(operation)


    async def async_list(
        self,
        state: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按可选状态和窗口读取订阅快照。"""

        async def operation(
            repository: SubscribeOper,
        ) -> builtins.list[SubscriptionSnapshot]:
            """读取并在当前 Session 中投影订阅列表。"""
            records = await repository.async_list(state, page=page, count=count)
            return [_project_subscription(record) for record in records]

        return await self._async_read(operation)

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按用户、状态、类型和窗口读取订阅快照。"""

        async def operation(
            repository: SubscribeOper,
        ) -> builtins.list[SubscriptionSnapshot]:
            """读取并在当前 Session 中投影用户订阅。"""
            records = await repository.async_list_by_username(
                username,
                state,
                mtype,
                page=page,
                count=count,
            )
            return [_project_subscription(record) for record in records]

        return await self._async_read(operation)

    async def async_count(
        self,
        state: Optional[str] = None,
        username: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> int:
        """按公开列表筛选范围返回订阅精确总数。"""
        return await self._async_read(
            lambda repository: repository.async_count(
                state=state,
                username=username,
                mtype=mtype,
            )
        )

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按规范媒体身份读取订阅快照。"""

        async def operation(
            repository: SubscribeOper,
        ) -> builtins.list[SubscriptionSnapshot]:
            """读取并在当前 Session 中投影媒体订阅。"""
            records = await repository.async_list_by_media_identity(media_source, media_id, music_type)
            return [_project_subscription(record) for record in records]

        return await self._async_read(operation)

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按标题和季读取订阅快照。"""

        async def operation(
            repository: SubscribeOper,
        ) -> builtins.list[SubscriptionSnapshot]:
            """读取并在当前 Session 中投影标题订阅。"""
            records = await repository.async_list_by_title(title, season)
            return [_project_subscription(record) for record in records]

        return await self._async_read(operation)

class TransactionalSubscriptionHistoryRepository:
    """以独立短 AsyncSession 实现 Agent 等后台入口的订阅历史查询。"""

    def __init__(
        self,
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """注入每次查询创建独立会话的异步作用域。"""
        self._async_session = async_session

    async def async_get(
        self,
        history_id: int,
    ) -> Optional[SubscriptionHistorySnapshot]:
        """在短 Session 内按主键读取并投影历史快照。"""
        async with self._async_session() as session:
            record = await SubscribeHistoryOper(session).async_get(history_id)
            return _project_history(record) if record is not None else None

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """在短 Session 内按类型分页读取并投影历史快照。"""
        async with self._async_session() as session:
            records = await SubscribeHistoryOper(session).async_list_by_type(
                mtype,
                page,
                count,
            )
            return [_project_history(record) for record in records]

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """在短 Session 内按类型和用户分页读取并投影历史快照。"""
        async with self._async_session() as session:
            records = await SubscribeHistoryOper(session).async_list_by_type_and_username(
                mtype,
                username,
                page,
                count,
            )
            return [_project_history(record) for record in records]

    async def async_count_by_type(self, mtype: str) -> int:
        """在短 Session 内统计指定媒体类型的历史数量。"""
        async with self._async_session() as session:
            return await SubscribeHistoryOper(session).async_count_by_type(mtype)

    async def async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """在短 Session 内统计指定媒体类型和 owner 的历史数量。"""
        async with self._async_session() as session:
            return await SubscribeHistoryOper(
                session
            ).async_count_by_type_and_username(mtype, username)


class SessionSubscriptionRepository:
    """复用调用方 Session，负责订阅查询投影和暂存且不提交。"""

    def __init__(self, session: Session | AsyncSession) -> None:
        """绑定请求或命令作用域持有的 Session。"""
        self._session = session
        self._repository = SubscribeOper(session)

    def _sync_repository(self) -> SubscribeOper:
        """返回同步 Oper，并拒绝会话类型混用。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("该订阅操作需要同步 Session")
        return self._repository

    def _async_repository(self) -> SubscribeOper:
        """返回异步 Oper，并拒绝会话类型混用。"""
        if not isinstance(self._session, AsyncSession):
            raise RuntimeError("该订阅操作需要异步 Session")
        return self._repository

    def get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """同步按主键读取订阅快照。"""
        record = self._sync_repository().get(subscribe_id)
        return _project_subscription(record) if record is not None else None

    def exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断完整媒体身份是否已有订阅。"""
        return bool(
            self._sync_repository().exists(
                identity.media_source,
                identity.media_id,
                identity.season,
                identity.episode_group,
                identity.music_type,
            )
        )

    def history_exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断完整媒体身份是否已有订阅历史。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("订阅历史查询需要同步 Session")
        return SubscribeHistoryOper(self._session).exists(
            identity.media_source,
            identity.media_id,
            identity.season,
            identity.episode_group,
            identity.music_type,
        )

    def get_by(
        self,
        identity: SubscriptionIdentity,
    ) -> Optional[SubscriptionSnapshot]:
        """同步按来源身份读取订阅快照。"""
        if identity.type is None:
            return None
        record = self._sync_repository().get_by(
            type=identity.type,
            media_source=identity.media_source,
            media_id=identity.media_id,
            season=identity.season,
            music_type=identity.music_type,
        )
        return _project_subscription(record) if record is not None else None

    def list(self, state: Optional[str] = None) -> builtins.list[SubscriptionSnapshot]:
        """同步按可选状态读取订阅快照。"""
        return [_project_subscription(record) for record in self._sync_repository().list(state)]

    async def async_get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """异步按主键读取订阅快照。"""
        record = await self._async_repository().async_get(subscribe_id)
        return _project_subscription(record) if record is not None else None

    async def async_list(
        self,
        state: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按可选状态和窗口读取订阅快照。"""
        records = await self._async_repository().async_list(
            state,
            page=page,
            count=count,
        )
        return [_project_subscription(record) for record in records]

    def list_for_reference_rewrite(self) -> builtins.list[SubscriptionSnapshot]:
        """同步锁定全部订阅，供跨表规则组引用事务稳定重写。"""
        session = self._session
        if not isinstance(session, Session):
            raise RuntimeError("规则组引用重写需要调用方提供同步 Session")
        records = session.execute(
            select(Subscribe).order_by(Subscribe.id).with_for_update()
        ).scalars().all()
        return [_project_subscription(record) for record in records]

    async def async_list_for_reference_rewrite(
        self,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步锁定全部订阅，供跨表规则组引用事务稳定重写。"""
        session = self._session
        if not isinstance(session, AsyncSession):
            raise RuntimeError("规则组引用重写需要调用方提供 AsyncSession")
        result = await session.execute(
            select(Subscribe).order_by(Subscribe.id).with_for_update()
        )
        return [_project_subscription(record) for record in result.scalars().all()]

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按用户、状态、类型和窗口读取订阅快照。"""
        records = await self._async_repository().async_list_by_username(
            username,
            state,
            mtype,
            page=page,
            count=count,
        )
        return [_project_subscription(record) for record in records]

    async def async_count(
        self,
        state: Optional[str] = None,
        username: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> int:
        """按公开列表筛选范围返回订阅精确总数。"""
        return await self._async_repository().async_count(
            state=state,
            username=username,
            mtype=mtype,
        )

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按规范媒体身份读取订阅快照。"""
        records = await self._async_repository().async_list_by_media_identity(media_source, media_id, music_type)
        return [_project_subscription(record) for record in records]

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按标题和季读取订阅快照。"""
        records = await self._async_repository().async_list_by_title(title, season)
        return [_project_subscription(record) for record in records]

    def stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """同步暂存新增订阅。"""
        result = self._sync_repository().stage_add(identity.to_payload(), payload.to_payload(), username)
        return SubscriptionWriteResult(result.subscribe_id, result.message, result.created)

    async def async_stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """异步暂存新增订阅。"""
        result = await self._async_repository().async_stage_add(identity.to_payload(), payload.to_payload(), username)
        return SubscriptionWriteResult(result.subscribe_id, result.message, result.created)

    def stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """同步暂存更新并返回事务内订阅快照。"""
        record = self._sync_repository().update(subscribe_id, patch.to_payload())
        return _project_subscription(record) if record is not None else None

    async def async_stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """异步暂存更新并返回事务内订阅快照。"""
        record = await self._async_repository().async_stage_update(subscribe_id, patch.to_payload())
        return _project_subscription(record) if record is not None else None

    async def get_candidate(
        self,
        subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """异步读取删除候选快照。"""
        snapshot = await self.async_get(subscribe_id)
        return self._candidate(snapshot)

    def get_candidate_sync(
        self,
        subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """同步读取删除候选快照。"""
        return self._candidate(self.get(subscribe_id))

    @staticmethod
    def _candidate(
        snapshot: Optional[SubscriptionSnapshot],
    ) -> Optional[SubscribeDeletionCandidate]:
        """把订阅快照裁剪为删除候选。"""
        if snapshot is None:
            return None
        return SubscribeDeletionCandidate(
            subscribe_id=snapshot.id,
            username=snapshot.username,
            event_payload=snapshot.to_dict(),
        )

    async def list_candidates_by_identity(
        self,
        identity: SubscriptionIdentity,
    ) -> builtins.list[SubscribeDeletionCandidate]:
        """异步按媒体身份读取去重删除候选。"""
        records = await self.async_list_by_media_identity(identity.media_source, identity.media_id, identity.music_type)
        candidates: builtins.list[SubscribeDeletionCandidate] = []
        for snapshot in records:
            if identity.season is not None and snapshot.season != identity.season:
                continue
            candidate = self._candidate(snapshot)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def list_search_ids(
        self,
        username: Optional[str],
        state: str,
    ) -> builtins.list[int]:
        """异步读取用户或管理员全局范围内指定状态的订阅主键。"""
        snapshots = (
            await self.async_list_by_username(username, state)
            if username is not None
            else await self.async_list(state)
        )
        return [snapshot.id for snapshot in snapshots]

    async def stage_delete(self, subscribe_id: int) -> None:
        """异步暂存删除订阅。"""
        await self._async_repository().stage_delete(subscribe_id)

    def stage_delete_sync(self, subscribe_id: int) -> None:
        """同步暂存删除订阅。"""
        self._sync_repository().stage_delete_sync(subscribe_id)

    def stage_history(self, payload: SubscriptionHistoryPatch) -> None:
        """同步暂存订阅历史。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("订阅历史暂存需要同步 Session")
        SubscribeHistoryOper(self._session).stage_add(payload.to_payload())


class SessionSubscriptionHistoryRepository:
    """复用请求 AsyncSession 的订阅历史查询与删除适配器。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定请求持有的 AsyncSession。"""
        self._repository = SubscribeHistoryOper(session)

    async def async_get(
        self,
        history_id: int,
    ) -> Optional[SubscriptionHistorySnapshot]:
        """异步按主键读取订阅历史快照。"""
        record = await self._repository.async_get(history_id)
        return _project_history(record) if record is not None else None

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """异步按类型分页读取订阅历史快照。"""
        records = await self._repository.async_list_by_type(mtype, page, count)
        return [_project_history(record) for record in records]

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """异步按类型和用户分页读取订阅历史快照。"""
        records = await self._repository.async_list_by_type_and_username(mtype, username, page, count)
        return [_project_history(record) for record in records]

    async def async_count_by_type(self, mtype: str) -> int:
        """在请求 Session 中统计指定媒体类型的订阅历史。"""
        return await self._repository.async_count_by_type(mtype)

    async def async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """在请求 Session 中统计指定类型和用户的订阅历史。"""
        return await self._repository.async_count_by_type_and_username(
            mtype,
            username,
        )

    async def stage_delete(self, history_id: int) -> None:
        """异步暂存删除订阅历史。"""
        await self._repository.async_delete(history_id)


class SessionSubscriptionBatchWriter:
    """使用请求级共享异步 Session 原子新增一组订阅并结算提交后效果。"""

    def __init__(
        self,
        *,
        repository: SubscriptionStagingPort,
        unit_of_work: AsyncUnitOfWork,
        outbox: AsyncSubscriptionOutboxStager,
        dispatch_store: AsyncOutboxDispatchStore,
    ) -> None:
        """注入同一请求事务的写端口和 durable intent 结算能力。"""
        self._command = AsyncCreateSubscriptionBatchCommand(
            repository=repository,
            unit_of_work=unit_of_work,
            outbox=outbox,
        )
        self._dispatch_store = dispatch_store

    async def async_add(
        self,
        requests: Sequence[SubscriptionCreateRequest],
    ) -> tuple[SubscriptionWriteResult, ...]:
        """提交整批订阅后逐条认领并结算与单条新增一致的 intents。"""

        async def settle(
            subscribe_id: int,
            request: SubscriptionCreateRequest,
        ) -> None:
            """在批量事务提交后结算一条订阅的事件和统计 intents。"""
            payload = request.payload.to_payload()
            notification = (
                dict(request.notification) if request.notification else None
            )

            async def invoke() -> bool | None:
                """复用准备阶段冻结的单条新增提交后副作用。"""
                if request.after_commit is None:
                    return True
                delivered = await request.after_commit(subscribe_id)
                return None if delivered is None else bool(delivered)

            await _deliver_added_effects_async(
                self._dispatch_store,
                subscribe_id,
                payload,
                notification,
                invoke,
                occurrence_id=request.occurrence_id,
            )

        return await self._command.execute(requests, after_commit=settle)


def _added_effect_keys(
    subscribe_id: int,
    payload: dict[str, JsonData],
    notification: Optional[dict[str, JsonData]],
    occurrence_id: str,
) -> tuple[str, ...]:
    """返回组合回调实际包含的独立 durable effect 键。"""
    keys = [
        subscription_added_event_key(
            subscribe_id,
            payload,
            occurrence_id=occurrence_id,
        )
    ]
    if notification:
        keys.append(
            subscription_added_notification_key(
                subscribe_id,
                payload,
                occurrence_id=occurrence_id,
            )
        )
    keys.append(
        subscription_added_report_key(
            subscribe_id,
            payload,
            occurrence_id=occurrence_id,
        )
    )
    return tuple(keys)


def _claim_added_effects(
    store: OutboxDispatchStore,
    keys: tuple[str, ...],
    now: datetime,
) -> Optional[tuple[ClaimedOutboxMessage, ...]]:
    """全量认领组合回调；竞争丢失时释放本次已取得的 lease。"""
    claimed: builtins.list[ClaimedOutboxMessage] = []
    for key in keys:
        message = store.claim_by_event_key(
            key,
            now,
            now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
        )
        if message is None:
            for owned in claimed:
                store.retry(
                    owned.message_id,
                    owned.attempt,
                    next_retry_at=now,
                    last_error="组合副作用由其他 owner 接管",
                    dead=False,
                )
            return None
        claimed.append(message)
    return tuple(claimed)


def _deliver_added_effects(
    store: OutboxDispatchStore,
    subscribe_id: int,
    payload: dict[str, JsonData],
    notification: Optional[dict[str, JsonData]],
    effect: Callable[[], Optional[bool]],
    *,
    occurrence_id: str,
) -> None:
    """认领组合回调并按事件、通知、统计的确认结果分别结算。"""
    now = datetime.now(timezone.utc)
    claimed = _claim_added_effects(
        store,
        _added_effect_keys(subscribe_id, payload, notification, occurrence_id),
        now,
    )
    if claimed is None:
        return
    try:
        report_delivered = effect()
    except Exception as error:
        for message in claimed:
            store.retry(
                message.message_id,
                message.attempt,
                next_retry_at=now,
                last_error=str(error)[:4000],
                dead=False,
            )
        raise
    for message in claimed[:-1]:
        if not store.complete(message.message_id, message.attempt, now):
            raise OutboxLeaseLostError("订阅新增完成凭证已失效")
    report = claimed[-1]
    if report_delivered is False:
        store.retry(
            report.message_id,
            report.attempt,
            next_retry_at=now,
            last_error="订阅新增统计未确认",
            dead=False,
        )
    else:
        if not store.complete(report.message_id, report.attempt, now):
            raise OutboxLeaseLostError("订阅新增统计完成凭证已失效")


async def _claim_added_effects_async(
    store: AsyncOutboxDispatchStore,
    keys: tuple[str, ...],
    now: datetime,
) -> Optional[tuple[ClaimedOutboxMessage, ...]]:
    """异步全量认领组合回调，竞争丢失时释放已取得 lease。"""
    claimed: builtins.list[ClaimedOutboxMessage] = []
    for key in keys:
        message = await store.claim_by_event_key(
            key,
            now,
            now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
        )
        if message is None:
            for owned in claimed:
                await store.retry(
                    owned.message_id,
                    owned.attempt,
                    next_retry_at=now,
                    last_error="组合副作用由其他 owner 接管",
                    dead=False,
                )
            return None
        claimed.append(message)
    return tuple(claimed)


async def _deliver_added_effects_async(
    store: AsyncOutboxDispatchStore,
    subscribe_id: int,
    payload: dict[str, JsonData],
    notification: Optional[dict[str, JsonData]],
    effect: Callable[[], Awaitable[bool | None]],
    *,
    occurrence_id: str,
) -> None:
    """异步认领组合回调并按各 intent 的确认结果分别结算。"""
    now = datetime.now(timezone.utc)
    claimed = await _claim_added_effects_async(
        store,
        _added_effect_keys(subscribe_id, payload, notification, occurrence_id),
        now,
    )
    if claimed is None:
        return
    try:
        report_delivered = await effect()
    except Exception as error:
        for message in claimed:
            await store.retry(
                message.message_id,
                message.attempt,
                next_retry_at=now,
                last_error=str(error)[:4000],
                dead=False,
            )
        raise
    for message in claimed[:-1]:
        if not await store.complete(message.message_id, message.attempt, now):
            raise OutboxLeaseLostError("订阅新增完成凭证已失效")
    report = claimed[-1]
    if report_delivered is False:
        await store.retry(
            report.message_id,
            report.attempt,
            next_retry_at=now,
            last_error="订阅新增统计未确认",
            dead=False,
        )
    else:
        if not await store.complete(report.message_id, report.attempt, now):
            raise OutboxLeaseLostError("订阅新增统计完成凭证已失效")
