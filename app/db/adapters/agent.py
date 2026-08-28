"""Agent 自主任务与插件数据的类型化持久化适配器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import List, Optional, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.agenttask import (
    AgentTaskFinishOutcome,
    AgentTaskRunSnapshot,
    AgentTaskSnapshot,
)
from app.db.oper.agenttask import AgentTaskOper
from app.db.oper.plugindata import PluginDataOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.common import JsonData

ResultT = TypeVar("ResultT")


def _project_task(record: object) -> AgentTaskSnapshot:
    """在 Session 内把自主任务 ORM 记录投影为冻结快照。"""
    task_id = getattr(record, "id", None)
    name = getattr(record, "name", None)
    content = getattr(record, "content", None)
    trigger_type = getattr(record, "trigger_type", None)
    user_id = getattr(record, "user_id", None)
    session_id = getattr(record, "session_id", None)
    created_at = getattr(record, "created_at", None)
    updated_at = getattr(record, "updated_at", None)
    if (
        not isinstance(task_id, int)
        or not isinstance(name, str)
        or not isinstance(content, str)
        or not isinstance(trigger_type, str)
        or not isinstance(user_id, str)
        or not isinstance(session_id, str)
        or not isinstance(created_at, str)
        or not isinstance(updated_at, str)
    ):
        raise ValueError("Agent 自主任务缺少稳定身份或必需字段")
    return AgentTaskSnapshot(
        id=task_id,
        name=name,
        content=content,
        trigger_type=trigger_type,
        cron_expression=getattr(record, "cron_expression", None),
        run_at=getattr(record, "run_at", None),
        enabled=bool(getattr(record, "enabled", False)),
        user_id=user_id,
        username=getattr(record, "username", None),
        session_id=session_id,
        channel=getattr(record, "channel", None),
        source=getattr(record, "source", None),
        original_chat_id=getattr(record, "original_chat_id", None),
        last_status=str(getattr(record, "last_status", "")),
        last_run_at=getattr(record, "last_run_at", None),
        last_result=getattr(record, "last_result", None),
        last_run_id=getattr(record, "last_run_id", None),
        run_count=int(getattr(record, "run_count", 0) or 0),
        created_at=created_at,
        updated_at=updated_at,
    )


def _project_run(record: object) -> AgentTaskRunSnapshot:
    """在 Session 内把自主任务运行 ORM 记录投影为冻结快照。"""
    run_id = getattr(record, "run_id", None)
    task_id = getattr(record, "task_id", None)
    trigger_source = getattr(record, "trigger_source", None)
    name = getattr(record, "name", None)
    content = getattr(record, "content", None)
    trigger_type = getattr(record, "trigger_type", None)
    user_id = getattr(record, "user_id", None)
    session_id = getattr(record, "session_id", None)
    status = getattr(record, "status", None)
    started_at = getattr(record, "started_at", None)
    if (
        not isinstance(run_id, str)
        or not isinstance(task_id, int)
        or not isinstance(trigger_source, str)
        or not isinstance(name, str)
        or not isinstance(content, str)
        or not isinstance(trigger_type, str)
        or not isinstance(user_id, str)
        or not isinstance(session_id, str)
        or not isinstance(status, str)
        or not isinstance(started_at, str)
    ):
        raise ValueError("Agent 自主任务运行记录缺少稳定身份或必需字段")
    return AgentTaskRunSnapshot(
        run_id=run_id,
        task_id=task_id,
        trigger_source=trigger_source,
        name=name,
        content=content,
        trigger_type=trigger_type,
        cron_expression=getattr(record, "cron_expression", None),
        run_at=getattr(record, "run_at", None),
        user_id=user_id,
        username=getattr(record, "username", None),
        session_id=session_id,
        channel=getattr(record, "channel", None),
        message_source=getattr(record, "message_source", None),
        original_chat_id=getattr(record, "original_chat_id", None),
        status=status,
        started_at=started_at,
        finished_at=getattr(record, "finished_at", None),
        result=getattr(record, "result", None),
    )


class SessionAgentTaskRepository:
    """把调用方同步 Session 适配为类型化自主任务仓储。"""

    def __init__(self, session: Session) -> None:
        """保存调用方独占的同步 Session。"""
        self._repository = AgentTaskOper(session)

    def add(self, **values: object) -> AgentTaskSnapshot | None:
        """暂存新增任务并返回冻结快照。"""
        record = self._repository.add(**values)
        return _project_task(record) if record is not None else None

    def get(
        self,
        task_id: int,
        user_id: Optional[str] = None,
    ) -> AgentTaskSnapshot | None:
        """读取一条任务并在 Session 内投影。"""
        record = self._repository.get(task_id=task_id, user_id=user_id)
        return _project_task(record) if record is not None else None

    def list(
        self,
        user_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> list[AgentTaskSnapshot]:
        """读取任务列表并在 Session 内投影。"""
        return [
            _project_task(record)
            for record in self._repository.list(user_id=user_id, enabled=enabled)
        ]

    def update(
        self,
        task_id: int,
        payload: dict[str, object],
        user_id: Optional[str] = None,
    ) -> bool:
        """暂存任务更新。"""
        return self._repository.update(task_id, payload, user_id)

    def delete(self, task_id: int, user_id: Optional[str] = None) -> bool:
        """暂存任务及运行记录删除。"""
        return self._repository.delete(task_id, user_id)

    def mark_interrupted(self, task_id: int, result: str) -> bool:
        """暂存遗留运行态收口。"""
        return self._repository.mark_interrupted(task_id, result)

    def begin_run(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        run_id: str | None = None,
    ) -> AgentTaskRunSnapshot | None:
        """原子认领任务并返回运行快照。"""
        record = self._repository.begin_run(
            task_id,
            trigger_source,
            run_id=run_id,
        )
        return _project_run(record) if record is not None else None

    def list_runs(
        self,
        task_id: int,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[AgentTaskRunSnapshot]:
        """读取最近运行并在 Session 内投影。"""
        return [
            _project_run(record)
            for record in self._repository.list_runs(task_id, user_id, limit)
        ]

    def finish_run_outcome(
        self,
        run_id: str,
        success: bool,
        result: str,
    ) -> AgentTaskFinishOutcome:
        """暂存精确运行收口并返回结构化事实。"""
        outcome = self._repository.finish_run_outcome(run_id, success, result)
        return AgentTaskFinishOutcome(
            run_finalized=outcome.run_finalized,
            task_projection_updated=outcome.task_projection_updated,
            date_task_disabled=outcome.date_task_disabled,
        )


class TransactionalAgentTaskRepository:
    """为 Scheduler 和 Agent 工具创建短生命周期自主任务事务。"""

    def __init__(self, session: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步 Session 工厂。"""
        self._session = session

    def _read(
        self,
        operation: Callable[[SessionAgentTaskRepository], ResultT],
    ) -> ResultT:
        """在独立 Session 内执行一次查询并完成投影。"""
        session = self._session()
        try:
            return operation(SessionAgentTaskRepository(session))
        finally:
            session.close()

    def _write(
        self,
        operation: Callable[[SessionAgentTaskRepository], ResultT],
    ) -> ResultT:
        """在独立短事务中执行一次写入。"""
        session = self._session()
        unit_of_work = SqlAlchemyUnitOfWork(session)
        try:
            result = operation(SessionAgentTaskRepository(session))
            unit_of_work.commit()
            return result
        except Exception:
            unit_of_work.rollback()
            raise
        finally:
            session.close()

    def add(self, **values: object) -> AgentTaskSnapshot | None:
        """新增任务并提交短事务。"""
        return self._write(lambda repository: repository.add(**values))

    def get(
        self,
        task_id: int,
        user_id: Optional[str] = None,
    ) -> AgentTaskSnapshot | None:
        """读取任务冻结快照。"""
        return self._read(lambda repository: repository.get(task_id, user_id))

    def list(
        self,
        user_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> list[AgentTaskSnapshot]:
        """读取任务冻结快照列表。"""
        return self._read(lambda repository: repository.list(user_id, enabled))

    def update(
        self,
        task_id: int,
        payload: dict[str, object],
        user_id: Optional[str] = None,
    ) -> bool:
        """更新任务并提交短事务。"""
        return self._write(
            lambda repository: repository.update(task_id, payload, user_id)
        )

    def delete(self, task_id: int, user_id: Optional[str] = None) -> bool:
        """删除任务并提交短事务。"""
        return self._write(lambda repository: repository.delete(task_id, user_id))

    def mark_interrupted(self, task_id: int, result: str) -> bool:
        """收口遗留运行态并提交短事务。"""
        return self._write(
            lambda repository: repository.mark_interrupted(task_id, result)
        )

    def begin_run(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        run_id: str | None = None,
    ) -> AgentTaskRunSnapshot | None:
        """认领任务并提交短事务。"""
        return self._write(
            lambda repository: repository.begin_run(
                task_id,
                trigger_source,
                run_id=run_id,
            )
        )

    def list_runs(
        self,
        task_id: int,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[AgentTaskRunSnapshot]:
        """读取任务最近运行快照。"""
        return self._read(
            lambda repository: repository.list_runs(task_id, user_id, limit)
        )

    def finish_run_outcome(
        self,
        run_id: str,
        success: bool,
        result: str,
    ) -> AgentTaskFinishOutcome:
        """收口运行并提交短事务。"""
        return self._write(
            lambda repository: repository.finish_run_outcome(
                run_id,
                success,
                result,
            )
        )


class TransactionalPluginDataRepository:
    """为 Agent 插件数据读取创建短生命周期异步 Session。"""

    def __init__(
        self,
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存由启动组合根提供的异步 Session 工厂。"""
        self._async_session = async_session

    async def get(self, plugin_id: str, key: str) -> JsonData:
        """读取单个键并复制 JSON 值。"""
        async with self._async_session() as session:
            value = await PluginDataOper(session).async_get_data(plugin_id, key)
            return cast(JsonData, deepcopy(value))

    async def list(self, plugin_id: str) -> dict[str, JsonData]:
        """读取全部键值并在 Session 内投影为普通字典。"""
        async with self._async_session() as session:
            rows = await PluginDataOper(session).async_get_data_all(plugin_id) or []
            return {
                str(row.key): cast(JsonData, deepcopy(row.value))
                for row in rows
            }
