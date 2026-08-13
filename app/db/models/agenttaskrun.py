from typing import Optional

from sqlalchemy import Column, Index, Integer, String, Text, update
from sqlalchemy.orm import Session

from app.db import Base, db_query, db_update, get_id_column
from app.db.models.agenttask import AgentTask


class AgentTaskRun(Base):
    """Agent 自主定时任务的一次真实执行记录。"""

    id = get_id_column()
    # 对外稳定的运行身份；内部自增主键不进入 Agent 合同
    run_id = Column(String, nullable=False)
    # 所属计划及触发入口
    task_id = Column(Integer, nullable=False)
    trigger_source = Column(String, nullable=False)
    # 执行开始时的任务与用户上下文快照
    name = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    trigger_type = Column(String, nullable=False)
    cron_expression = Column(String)
    run_at = Column(String)
    user_id = Column(String, nullable=False)
    username = Column(String)
    session_id = Column(String, nullable=False)
    channel = Column(String)
    message_source = Column(String)
    original_chat_id = Column(String)
    # running-success/failed/interrupted；取消沿用 failed 和明确结果文本
    status = Column(String, nullable=False)
    started_at = Column(String, nullable=False)
    finished_at = Column(String)
    result = Column(Text)

    __table_args__ = (
        Index("ix_agenttaskrun_run_id", "run_id", unique=True),
        Index("ix_agenttaskrun_task_started", "task_id", "started_at", "id"),
    )

    @classmethod
    @db_update
    def begin_run(
            cls,
            db: Session,
            task_id: int,
            run_id: str,
            trigger_source: str,
            started_at: str,
    ) -> Optional[str]:
        """原子认领可执行任务并创建对应的运行记录。"""
        if trigger_source not in {"scheduled", "manual"}:
            raise ValueError(f"不支持的 Agent 任务触发来源：{trigger_source}")
        # 认领和快照读取必须是同一条语句，配置更新与执行开始才能共享同一行级顺序。
        claimed = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.enabled.is_(True),
                AgentTask.last_status != "running",
            )
            .values({
                "last_status": "running",
                "last_run_at": started_at,
                "last_run_id": run_id,
                "updated_at": started_at,
            })
            .returning(
                AgentTask.id,
                AgentTask.name,
                AgentTask.content,
                AgentTask.trigger_type,
                AgentTask.cron_expression,
                AgentTask.run_at,
                AgentTask.user_id,
                AgentTask.username,
                AgentTask.session_id,
                AgentTask.channel,
                AgentTask.source,
                AgentTask.original_chat_id,
            )
            .execution_options(synchronize_session=False)
        ).mappings().first()
        if not claimed:
            return None
        db.add(cls(
            run_id=run_id,
            task_id=claimed["id"],
            trigger_source=trigger_source,
            name=claimed["name"],
            content=claimed["content"],
            trigger_type=claimed["trigger_type"],
            cron_expression=claimed["cron_expression"],
            run_at=claimed["run_at"],
            user_id=claimed["user_id"],
            username=claimed["username"],
            session_id=claimed["session_id"],
            channel=claimed["channel"],
            message_source=claimed["source"],
            original_chat_id=claimed["original_chat_id"],
            status="running",
            started_at=started_at,
        ))
        db.flush()
        return run_id

    @classmethod
    @db_update
    def finish_run(
            cls,
            db: Session,
            run_id: str,
            success: bool,
            result: str,
            finished_at: str,
            disable_date_task: bool = False,
    ) -> bool:
        """原子收口精确运行，并仅在仍为最新运行时更新任务投影。"""
        run = db.query(cls).filter(
            cls.run_id == run_id,
        ).first()
        if not run:
            return False
        status = "success" if success else "failed"
        finalized = db.query(cls).filter(
            cls.run_id == run_id,
            cls.status == "running",
        ).update(
            {
                "status": status,
                "result": result,
                "finished_at": finished_at,
            },
            synchronize_session=False,
        )
        if not finalized:
            return False

        task = db.query(AgentTask).filter(
            AgentTask.id == run.task_id,
            AgentTask.last_run_id == run_id,
        ).first()
        if task:
            payload = {
                "last_status": status,
                "last_result": result,
                "run_count": AgentTask.run_count + 1,
                "updated_at": finished_at,
            }
            if (
                    disable_date_task
                    and run.trigger_type == "date"
                    and task.trigger_type == run.trigger_type
                    and task.run_at == run.run_at
            ):
                payload["enabled"] = False
            db.query(AgentTask).filter(
                AgentTask.id == run.task_id,
                AgentTask.last_run_id == run_id,
            ).update(payload, synchronize_session=False)
        return True

    @classmethod
    @db_update
    def interrupt_task(
            cls,
            db: Session,
            task_id: int,
            result: str,
            finished_at: str,
    ) -> bool:
        """原子标记冷启动时遗留的最新运行及任务投影为结果未知。"""
        task = db.query(AgentTask).filter(
            AgentTask.id == task_id,
            AgentTask.last_status == "running",
        ).first()
        if not task:
            return False
        if task.last_run_id:
            interrupted = db.query(cls).filter(
                cls.run_id == task.last_run_id,
                cls.task_id == task.id,
                cls.status == "running",
            ).update(
                {
                    "status": "interrupted",
                    "result": result,
                    "finished_at": finished_at,
                },
                synchronize_session=False,
            )
            if not interrupted:
                return False
        return bool(db.query(AgentTask).filter(
            AgentTask.id == task.id,
            AgentTask.last_status == "running",
            AgentTask.last_run_id == task.last_run_id,
        ).update(
            {
                "last_status": "interrupted",
                "last_result": result,
                "updated_at": finished_at,
            },
            synchronize_session=False,
        ))

    @classmethod
    @db_update
    def delete_task_and_runs(
            cls,
            db: Session,
            task_id: int,
            user_id: Optional[str] = None,
    ) -> bool:
        """原子删除非运行中任务及其执行历史。"""
        query = db.query(AgentTask).filter(
            AgentTask.id == task_id,
            AgentTask.last_status != "running",
        )
        if user_id is not None:
            query = query.filter(AgentTask.user_id == user_id)
        deleted = query.delete(synchronize_session=False)
        if not deleted:
            return False
        db.query(cls).filter(cls.task_id == task_id).delete(synchronize_session=False)
        return True

    @classmethod
    @db_query
    def get_by_run_id(
            cls,
            db: Session,
            run_id: str,
    ) -> Optional["AgentTaskRun"]:
        """按公开运行 ID 查询一次执行。"""
        return db.query(cls).filter(cls.run_id == run_id).first()

    @classmethod
    @db_query
    def list_for_task(
            cls,
            db: Session,
            task_id: int,
            user_id: Optional[str] = None,
            limit: int = 10,
    ) -> list["AgentTaskRun"]:
        """按父任务 owner 校验后返回最近的有界运行历史。"""
        query = db.query(cls).join(AgentTask, AgentTask.id == cls.task_id).filter(
            cls.task_id == task_id,
        )
        if user_id is not None:
            query = query.filter(AgentTask.user_id == user_id)
        return query.order_by(cls.started_at.desc(), cls.id.desc()).limit(limit).all()
