"""统一数据维护对追加型历史表的安全清理测试。"""

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.db.base import Base
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.agentchat import AgentChat
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun
from app.db.models.subscribehistory import SubscribeHistory


def _cleanup_policy() -> CleanupPolicy:
    """只启用本组新增历史表的 30 天保留期。"""
    return CleanupPolicy(
        enabled=True,
        message_days=0,
        download_history_days=0,
        site_userdata_days=0,
        transfer_history_days=0,
        download_failure_days=0,
        subscribe_history_days=30,
        agent_chat_days=30,
        agent_task_run_days=30,
        outbox_completed_days=0,
        outbox_dead_days=0,
    )


def test_growth_table_cleanup_preserves_live_agent_recovery_state() -> None:
    """旧历史可回收，但任务引用会话、最后运行和运行中记录必须保留。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    old_time = "2026-06-01 12:00:00"
    recent_time = "2026-08-20 12:00:00"

    with factory() as session:
        session.add_all([
            SubscribeHistory(name="old-subscribe", date=old_time),
            SubscribeHistory(name="recent-subscribe", date=recent_time),
            AgentChat(
                session_id="old-unreferenced",
                title="old-unreferenced",
                created_at=old_time,
                updated_at=old_time,
            ),
            AgentChat(
                session_id="task-context",
                title="task-context",
                created_at=old_time,
                updated_at=old_time,
            ),
            AgentChat(
                session_id="recent-chat",
                title="recent-chat",
                created_at=recent_time,
                updated_at=recent_time,
            ),
        ])
        task = AgentTask(
            name="cleanup-protected-task",
            content="test",
            trigger_type="cron",
            enabled=True,
            user_id="1",
            session_id="task-context",
            last_status="success",
            last_run_id="latest-run",
            run_count=2,
            created_at=old_time,
            updated_at=old_time,
        )
        session.add(task)
        session.flush()
        session.add_all([
            AgentTaskRun(
                run_id="expired-run",
                task_id=task.id,
                trigger_source="scheduled",
                name=task.name,
                content=task.content,
                trigger_type=task.trigger_type,
                user_id=task.user_id,
                session_id=task.session_id,
                status="success",
                started_at=old_time,
                finished_at=old_time,
            ),
            AgentTaskRun(
                run_id="latest-run",
                task_id=task.id,
                trigger_source="scheduled",
                name=task.name,
                content=task.content,
                trigger_type=task.trigger_type,
                user_id=task.user_id,
                session_id=task.session_id,
                status="success",
                started_at=old_time,
                finished_at=old_time,
            ),
            AgentTaskRun(
                run_id="running-run",
                task_id=task.id,
                trigger_source="manual",
                name=task.name,
                content=task.content,
                trigger_type=task.trigger_type,
                user_id=task.user_id,
                session_id=task.session_id,
                status="running",
                started_at=old_time,
            ),
            AgentTaskRun(
                run_id="recent-run",
                task_id=task.id,
                trigger_source="manual",
                name=task.name,
                content=task.content,
                trigger_type=task.trigger_type,
                user_id=task.user_id,
                session_id=task.session_id,
                status="failed",
                started_at=recent_time,
                finished_at=recent_time,
            ),
        ])
        session.commit()

    report = DataCleanupService(
        repository=DatabaseCleanupRepository(session_factory=factory),
        policy_reader=_cleanup_policy,
        clock=lambda: datetime(2026, 8, 26, 12, 0, 0),
    ).execute(batch_size=1)

    assert report["tables"]["subscribehistory"]["deleted"] == 1
    assert report["tables"]["agentchat"]["deleted"] == 1
    assert report["tables"]["agenttaskrun"]["deleted"] == 1
    with factory() as session:
        assert set(session.execute(select(SubscribeHistory.name)).scalars()) == {
            "recent-subscribe"
        }
        assert set(session.execute(select(AgentChat.session_id)).scalars()) == {
            "task-context",
            "recent-chat",
        }
        assert set(session.execute(select(AgentTaskRun.run_id)).scalars()) == {
            "latest-run",
            "running-run",
            "recent-run",
        }
