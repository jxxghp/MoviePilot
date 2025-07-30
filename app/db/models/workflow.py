from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, JSON, Sequence, String, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, db_query, db_update, async_db_query, async_db_update


class Workflow(Base):
    """
    工作流表
    """
    # ID
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 名称
    name = Column(String, index=True, nullable=False)
    # 描述
    description = Column(String)
    # 定时器
    timer = Column(String)
    # 触发类型：timer-定时触发 event-事件触发 manual-手动触发
    trigger_type = Column(String, default='timer')
    # 事件类型（当trigger_type为event时使用）
    event_type = Column(String)
    # 事件条件（JSON格式，用于过滤事件）
    event_conditions = Column(JSON, default=dict)
    # 状态：W-等待 R-运行中 P-暂停 S-成功 F-失败
    state = Column(String, nullable=False, index=True, default='W')
    # 已执行动作（,分隔）
    current_action = Column(String)
    # 任务执行结果
    result = Column(String)
    # 已执行次数
    run_count = Column(Integer, default=0)
    # 任务列表
    actions = Column(JSON, default=list)
    # 任务流
    flows = Column(JSON, default=list)
    # 执行上下文
    context = Column(JSON, default=dict)
    # 创建时间
    add_time = Column(String, default=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    # 最后执行时间
    last_time = Column(String)

    @staticmethod
    @db_query
    def list(db):
        return db.query(Workflow).all()

    @staticmethod
    @async_db_query
    async def async_list(db: AsyncSession):
        result = await db.execute(select(Workflow))
        return result.scalars().all()

    @staticmethod
    @db_query
    def get_enabled_workflows(db):
        return db.query(Workflow).filter(Workflow.state != 'P').all()

    @staticmethod
    @async_db_query
    async def async_get_enabled_workflows(db: AsyncSession):
        result = await db.execute(select(Workflow).where(Workflow.state != 'P'))
        return result.scalars().all()

    @staticmethod
    @db_query
    def get_timer_triggered_workflows(db):
        """获取定时触发的工作流"""
        return db.query(Workflow).filter(
            and_(
                or_(
                    Workflow.trigger_type == 'timer',
                    not Workflow.trigger_type
                ),
                Workflow.state != 'P'
            )
        ).all()

    @staticmethod
    @async_db_query
    async def async_get_timer_triggered_workflows(db: AsyncSession):
        """异步获取定时触发的工作流"""
        result = await db.execute(select(Workflow).where(
            and_(
                or_(
                    Workflow.trigger_type == 'timer',
                    not Workflow.trigger_type
                ),
                Workflow.state != 'P'
            )
        ))
        return result.scalars().all()

    @staticmethod
    @db_query
    def get_event_triggered_workflows(db):
        """获取事件触发的工作流"""
        return db.query(Workflow).filter(
            and_(
                Workflow.trigger_type == 'event',
                Workflow.state != 'P'
            )
        ).all()

    @staticmethod
    @async_db_query
    async def async_get_event_triggered_workflows(db: AsyncSession):
        """异步获取事件触发的工作流"""
        result = await db.execute(select(Workflow).where(
            and_(
                Workflow.trigger_type == 'event',
                Workflow.state != 'P'
            )
        ))
        return result.scalars().all()

    @staticmethod
    @db_query
    def get_by_name(db, name: str):
        return db.query(Workflow).filter(Workflow.name == name).first()

    @staticmethod
    @async_db_query
    async def async_get_by_name(db: AsyncSession, name: str):
        result = await db.execute(select(Workflow).where(Workflow.name == name))
        return result.scalars().first()

    @staticmethod
    @db_update
    def update_state(db, wid: int, state: str):
        db.query(Workflow).filter(Workflow.id == wid).update({"state": state})
        return True

    @staticmethod
    @async_db_update
    async def async_update_state(db: AsyncSession, wid: int, state: str):
        from sqlalchemy import update
        await db.execute(update(Workflow).where(Workflow.id == wid).values(state=state))
        return True

    @staticmethod
    @db_update
    def start(db, wid: int):
        db.query(Workflow).filter(Workflow.id == wid).update({
            "state": 'R'
        })
        return True

    @staticmethod
    @async_db_update
    async def async_start(db: AsyncSession, wid: int):
        from sqlalchemy import update
        await db.execute(update(Workflow).where(Workflow.id == wid).values(state='R'))
        return True

    @staticmethod
    @db_update
    def fail(db, wid: int, result: str):
        db.query(Workflow).filter(and_(Workflow.id == wid, Workflow.state != "P")).update({
            "state": 'F',
            "result": result,
            "last_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        return True

    @staticmethod
    @async_db_update
    async def async_fail(db: AsyncSession, wid: int, result: str):
        from sqlalchemy import update
        await db.execute(update(Workflow).where(
            and_(Workflow.id == wid, Workflow.state != "P")
        ).values(
            state='F',
            result=result,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @staticmethod
    @db_update
    def success(db, wid: int, result: Optional[str] = None):
        db.query(Workflow).filter(and_(Workflow.id == wid, Workflow.state != "P")).update({
            "state": 'S',
            "result": result,
            "run_count": Workflow.run_count + 1,
            "last_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        return True

    @staticmethod
    @async_db_update
    async def async_success(db: AsyncSession, wid: int, result: Optional[str] = None):
        from sqlalchemy import update
        await db.execute(update(Workflow).where(
            and_(Workflow.id == wid, Workflow.state != "P")
        ).values(
            state='S',
            result=result,
            run_count=Workflow.run_count + 1,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @staticmethod
    @db_update
    def reset(db, wid: int, reset_count: Optional[bool] = False):
        db.query(Workflow).filter(Workflow.id == wid).update({
            "state": 'W',
            "result": None,
            "current_action": None,
            "run_count": 0 if reset_count else Workflow.run_count,
        })
        return True

    @staticmethod
    @async_db_update
    async def async_reset(db: AsyncSession, wid: int, reset_count: Optional[bool] = False):
        from sqlalchemy import update
        await db.execute(update(Workflow).where(Workflow.id == wid).values(
            state='W',
            result=None,
            current_action=None,
            run_count=0 if reset_count else Workflow.run_count,
        ))
        return True

    @staticmethod
    @db_update
    def update_current_action(db, wid: int, action_id: str, context: dict):
        db.query(Workflow).filter(Workflow.id == wid).update({
            "current_action": Workflow.current_action + f",{action_id}" if Workflow.current_action else action_id,
            "context": context
        })
        return True

    @staticmethod
    @async_db_update
    async def async_update_current_action(db: AsyncSession, wid: int, action_id: str, context: dict):
        from sqlalchemy import update
        # 先获取当前current_action
        result = await db.execute(select(Workflow.current_action).where(Workflow.id == wid))
        current_action = result.scalar()
        new_current_action = current_action + f",{action_id}" if current_action else action_id
        
        await db.execute(update(Workflow).where(Workflow.id == wid).values(
            current_action=new_current_action,
            context=context
        ))
        return True
