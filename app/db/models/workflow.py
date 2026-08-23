from datetime import datetime
from builtins import list as builtin_list
from typing import Any, Optional

from sqlalchemy import Integer, JSON, String, Index, and_, or_, select, update
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base, get_id_column
from app.db.decorators import legacy_async_db_query, legacy_db_query


class Workflow(Base):
    """
    工作流表
    """
    # ID
    id = get_id_column()
    # 名称
    name: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # 描述
    description: Mapped[Optional[str]] = mapped_column(String)
    # 定时器
    timer: Mapped[Optional[str]] = mapped_column(String)
    # 触发类型：timer-定时触发 event-事件触发 manual-手动触发
    trigger_type: Mapped[Optional[str]] = mapped_column(String, default='timer')
    # 事件类型（当trigger_type为event时使用）
    event_type: Mapped[Optional[str]] = mapped_column(String)
    # 事件条件（JSON格式，用于过滤事件）
    event_conditions: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 状态：W-等待 R-运行中 P-暂停 S-成功 F-失败
    state: Mapped[str] = mapped_column(String, nullable=False, index=True, default='W')
    # 已执行动作（,分隔）
    current_action: Mapped[Optional[str]] = mapped_column(String)
    # 任务执行结果
    result: Mapped[Optional[str]] = mapped_column(String)
    # 已执行次数
    run_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 任务列表
    actions: Mapped[Optional[Any]] = mapped_column(JSON, default=builtin_list)
    # 任务流
    flows: Mapped[Optional[Any]] = mapped_column(JSON, default=builtin_list)
    # 执行上下文
    context: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 执行配置
    execution_config: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 结构化执行状态
    execution_state: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 创建时间
    add_time: Mapped[Optional[str]] = mapped_column(String, default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    # 最后执行时间
    last_time: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        Index('ix_workflow_trigger_type_state', 'trigger_type', 'state'),
    )

    @classmethod
    @legacy_db_query
    def get_enabled_workflows(cls, db):
        return list(db.execute(select(cls).where(cls.state != 'P')).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_get_enabled_workflows(cls, db: AsyncSession):
        result = await db.execute(select(cls).where(cls.state != 'P'))
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def get_timer_triggered_workflows(cls, db):
        """获取定时触发的工作流"""
        return list(db.execute(select(cls).where(
            and_(
                or_(
                    cls.trigger_type == 'timer',
                    cls.trigger_type.is_(None)
                ),
                cls.state != 'P'
            )
        )).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_get_timer_triggered_workflows(cls, db: AsyncSession):
        """异步获取定时触发的工作流"""
        result = await db.execute(select(cls).where(
            and_(
                or_(
                    cls.trigger_type == 'timer',
                    cls.trigger_type.is_(None)
                ),
                cls.state != 'P'
            )
        ))
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def get_event_triggered_workflows(cls, db):
        """获取事件触发的工作流"""
        return list(db.execute(select(cls).where(
            and_(
                cls.trigger_type == 'event',
                cls.state != 'P'
            )
        )).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_get_event_triggered_workflows(cls, db: AsyncSession):
        """异步获取事件触发的工作流"""
        result = await db.execute(select(cls).where(
            and_(
                cls.trigger_type == 'event',
                cls.state != 'P'
            )
        ))
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def get_by_name(cls, db, name: str):
        return db.execute(select(cls).where(cls.name == name)).scalars().first()

    @classmethod
    @legacy_async_db_query
    async def async_get_by_name(cls, db: AsyncSession, name: str):
        result = await db.execute(select(cls).where(cls.name == name))
        return result.scalars().first()

    @classmethod
    def update_state(cls, db, wid: int, state: str):
        db.execute(update(cls).where(cls.id == wid).values(state=state))
        return True

    @classmethod
    async def async_update_state(cls, db: AsyncSession, wid: int, state: str):
        """在调用方持有的异步事务中暂存工作流状态。"""
        await db.execute(update(cls).where(cls.id == wid).values(state=state))
        return True

    @classmethod
    def start(cls, db, wid: int):
        db.execute(update(cls).where(cls.id == wid).values(state='R'))
        return True

    @classmethod
    async def async_start(cls, db: AsyncSession, wid: int):
        """在调用方持有的异步事务中暂存运行中状态。"""
        await db.execute(update(cls).where(cls.id == wid).values(state='R'))
        return True

    @classmethod
    def fail(cls, db, wid: int, result: str):
        db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='F',
            result=result,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    async def async_fail(cls, db: AsyncSession, wid: int, result: str):
        """在调用方持有的异步事务中暂存失败结果。"""
        await db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='F',
            result=result,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    def success(cls, db, wid: int, result: Optional[str] = None):
        db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='S',
            result=result,
            run_count=cls.run_count + 1,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    async def async_success(cls, db: AsyncSession, wid: int, result: Optional[str] = None):
        """在调用方持有的异步事务中暂存成功结果。"""
        await db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='S',
            result=result,
            run_count=cls.run_count + 1,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    def reset(cls, db, wid: int, reset_count: Optional[bool] = False):
        db.execute(update(cls).where(cls.id == wid).values(
            state='W',
            result=None,
            current_action=None,
            context={},
            execution_state={},
            run_count=0 if reset_count else cls.run_count,
        ))
        return True

    @classmethod
    async def async_reset(cls, db: AsyncSession, wid: int, reset_count: Optional[bool] = False):
        """在调用方持有的异步事务中暂存执行状态重置。"""
        await db.execute(update(cls).where(cls.id == wid).values(
            state='W',
            result=None,
            current_action=None,
            context={},
            execution_state={},
            run_count=0 if reset_count else cls.run_count,
        ))
        return True

    @classmethod
    def update_current_action(cls, db, wid: int, action_id: str, context: dict,
                              execution_state: Optional[dict] = None):
        workflow = db.execute(select(cls).where(cls.id == wid)).scalars().first()
        current_actions = []
        if workflow and workflow.current_action:
            current_actions = [item for item in workflow.current_action.split(",") if item]
        if action_id and action_id not in current_actions:
            current_actions.append(action_id)
        update_values = {
            "current_action": ",".join(current_actions),
            "context": context
        }
        if execution_state is not None:
            update_values["execution_state"] = execution_state
        db.execute(update(cls).where(cls.id == wid).values(**update_values))
        return True

    @classmethod
    async def async_update_current_action(cls, db: AsyncSession, wid: int, action_id: str, context: dict,
                                          execution_state: Optional[dict] = None):
        """在调用方持有的异步事务中暂存动作进度。"""
        # 先获取当前current_action
        result = await db.execute(select(cls.current_action).where(cls.id == wid))
        current_action = result.scalar()
        current_actions = [item for item in (current_action or "").split(",") if item]
        if action_id and action_id not in current_actions:
            current_actions.append(action_id)
        new_current_action = ",".join(current_actions)

        update_values = {
            "current_action": new_current_action,
            "context": context
        }
        if execution_state is not None:
            update_values["execution_state"] = execution_state
        await db.execute(update(cls).where(cls.id == wid).values(**update_values))
        return True
