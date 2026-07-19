import json
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.agent import AgentChain, AgentManager, ReplyMode
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.create_agent_task import (
    CreateAgentTaskInput,
    CreateAgentTaskTool,
)
from app.agent.tools.impl.delete_agent_task import DeleteAgentTaskTool
from app.agent.tools.impl.query_agent_tasks import QueryAgentTasksTool
from app.agent.tools.impl.update_agent_task import UpdateAgentTaskTool
from app.core.config import settings
from app.db.agenttask_oper import AgentTaskOper
from app.scheduler import Scheduler
from app.utils.timer import TimerUtils


class _FakeAgentTaskScheduler:
    """记录 Agent 定时任务工具触发的运行时调度变更。"""

    def __init__(self) -> None:
        """初始化运行时调度记录。"""
        self.updated = []
        self.removed = []

    def update_agent_task_job(self, task_id: int) -> str:
        """记录任务重载并返回固定的下一次执行时间。"""
        self.updated.append(task_id)
        return "2099-01-01T00:00:00+08:00"

    def remove_agent_task_job(self, task_id: int) -> None:
        """记录任务移除。"""
        self.removed.append(task_id)

    def get_agent_task_next_run(self, task_id: int) -> str:
        """返回固定的下一次执行时间。"""
        return "2099-01-01T00:00:00+08:00"


@pytest.fixture
def anyio_backend() -> str:
    """限定异步用例使用项目 Agent 运行时采用的 asyncio 后端。"""
    return "asyncio"


@pytest.fixture(autouse=True)
def enable_ai_agent(monkeypatch) -> None:
    """在当前测试模块中启用 Agent 调度能力并在用例后自动还原。"""
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)


def _future_time(minutes: int = 10) -> str:
    """生成系统时区内的未来时间字符串。"""
    timezone = pytz.timezone(settings.TZ)
    return (datetime.now(timezone) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


def _build_tool(tool_class, user_id: str):
    """构造带当前用户消息上下文的 Agent 工具。"""
    tool = tool_class(session_id=f"session-{user_id}", user_id=user_id)
    tool.set_message_attr(
        channel="Telegram",
        source="telegram-test",
        username="admin",
    )
    tool.set_agent_context({"is_admin": True})
    return tool


def test_timer_utils_validates_date_and_cron_triggers() -> None:
    """自主任务时间工具应规范化单次时间并校验五段 cron。"""
    trigger_type, trigger = TimerUtils.normalize_schedule_trigger(
        trigger_type="date",
        trigger_value=_future_time(),
        timezone_name=settings.TZ,
        require_future=True,
    )
    assert trigger_type == "date"
    assert datetime.fromisoformat(trigger).tzinfo is not None

    trigger_type, trigger = TimerUtils.normalize_schedule_trigger(
        trigger_type="cron",
        trigger_value="  30   20  * * * ",
        timezone_name=settings.TZ,
    )
    assert trigger_type == "cron"
    assert trigger == "30 20 * * *"

    with pytest.raises(ValueError, match="标准五段"):
        TimerUtils.normalize_schedule_trigger(
            trigger_type="cron",
            trigger_value="30 20 * *",
            timezone_name=settings.TZ,
        )


def test_agent_task_tools_are_registered_with_relative_delay_schema() -> None:
    """工具工厂应公开完整任务管理工具，并声明相对分钟参数。"""
    tool_names = {
        tool_class.model_fields["name"].default
        for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES
    }
    assert {
        "create_agent_task",
        "query_agent_tasks",
        "update_agent_task",
        "delete_agent_task",
    }.issubset(tool_names)
    assert "delay_minutes" in CreateAgentTaskInput.model_json_schema()["properties"]


def test_agent_task_oper_persists_and_scopes_tasks() -> None:
    """AgentTaskOper 应持久化任务并按创建用户隔离查询和修改。"""
    user_id = f"user-{uuid4().hex}"
    oper = AgentTaskOper()
    task = oper.add(
        name="检查资源",
        content="检查电影资源",
        trigger_type="cron",
        cron_expression="0 */2 * * *",
        run_at=None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-1",
    )

    assert oper.get(task.id, user_id=user_id).content == "检查电影资源"
    assert oper.get(task.id, user_id="another-user") is None
    assert [item.id for item in oper.list(user_id=user_id)] == [task.id]

    assert oper.update(
        task_id=task.id,
        user_id=user_id,
        payload={"content": "检查更新后的资源", "unknown": "ignored"},
    )
    assert oper.get(task.id).content == "检查更新后的资源"
    assert oper.mark_running(task.id)
    assert not oper.mark_running(task.id)
    assert oper.finish(task.id, success=True, result="完成")
    assert not oper.delete(task.id, user_id="another-user")
    assert oper.delete(task.id, user_id=user_id)


def test_scheduler_registers_and_removes_agent_task_job() -> None:
    """Scheduler 应把数据库任务注册为精确 APScheduler Job 并可动态移除。"""
    user_id = f"user-{uuid4().hex}"
    task = AgentTaskOper().add(
        name="定时检查",
        content="检查资源",
        trigger_type="date",
        cron_expression=None,
        run_at=_future_time(),
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-1",
    )
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._scheduler = BackgroundScheduler(timezone=settings.TZ)

    next_run_at = scheduler.update_agent_task_job(task.id)
    job_id = scheduler._get_agent_task_job_id(task.id)

    assert next_run_at
    assert scheduler._scheduler.get_job(job_id) is not None
    assert scheduler._jobs[job_id]["kwargs"] == {"task_id": task.id}

    scheduler.remove_agent_task_job(task.id)
    assert scheduler._scheduler.get_job(job_id) is None
    assert job_id not in scheduler._jobs


@pytest.mark.anyio
async def test_agent_task_tools_manage_persistent_schedule(monkeypatch) -> None:
    """Agent 管理工具应完成创建、查询、暂停、修改和删除闭环。"""
    user_id = f"user-{uuid4().hex}"
    fake_scheduler = _FakeAgentTaskScheduler()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: fake_scheduler)

    create_tool = _build_tool(CreateAgentTaskTool, user_id)
    created = json.loads(
        await create_tool.run(
            name="十分钟后检查",
            content="检查示例电影是否有资源，不要自动下载",
            trigger_type="date",
            delay_minutes=10,
        )
    )
    task_id = created["id"]
    assert created["enabled"] is True
    assert datetime.fromisoformat(created["run_at"]) > datetime.now(
        pytz.timezone(settings.TZ)
    )
    assert created["next_run_at"] == "2099-01-01T00:00:00+08:00"
    assert fake_scheduler.updated == [task_id]

    query_tool = _build_tool(QueryAgentTasksTool, user_id)
    queried = json.loads(await query_tool.run(task_id=task_id))
    assert queried["total"] == 1
    assert queried["tasks"][0]["content"].startswith("检查示例电影")

    update_tool = _build_tool(UpdateAgentTaskTool, user_id)
    updated = json.loads(
        await update_tool.run(
            task_id=task_id,
            content="检查示例电影是否有 4K 资源，不要自动下载",
            trigger_type="cron",
            trigger="*/15 * * * *",
            enabled=True,
        )
    )
    assert updated["trigger_type"] == "cron"
    assert updated["cron_expression"] == "*/15 * * * *"
    assert updated["run_at"] is None

    assert AgentTaskOper().mark_running(task_id)
    running_update = await update_tool.run(task_id=task_id, enabled=False)
    assert running_update == f"Agent 定时任务 {task_id} 正在执行，请稍后再修改"
    AgentTaskOper().finish(task_id, success=True, result="完成")

    deleted = await _build_tool(DeleteAgentTaskTool, user_id).run(task_id=task_id)
    assert deleted == f"Agent 定时任务 {task_id} 已删除"
    assert fake_scheduler.removed == [task_id]


@pytest.mark.anyio
async def test_agent_manager_executes_task_in_original_session() -> None:
    """定时触发应复用原 Agent 会话和渠道，并在单次执行后停用任务。"""
    user_id = f"user-{uuid4().hex}"
    task = AgentTaskOper().add(
        name="检查电影资源",
        content="搜索示例电影是否已有资源",
        trigger_type="date",
        cron_expression=None,
        run_at=_future_time(),
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-123",
    )
    manager = AgentManager()
    manager.process_message = AsyncMock(return_value="已找到 2 个资源")

    success, result = await manager.execute_scheduled_task(task.id)

    assert success is True
    assert result == "已找到 2 个资源"
    kwargs = manager.process_message.await_args.kwargs
    assert kwargs["session_id"] == task.session_id
    assert kwargs["user_id"] == user_id
    assert kwargs["channel"] == "Telegram"
    assert kwargs["source"] == "telegram-test"
    assert kwargs["original_chat_id"] == "chat-123"
    assert kwargs["reply_mode"] == ReplyMode.DISPATCH
    assert kwargs["wait_for_completion"] is True
    assert "搜索示例电影是否已有资源" in kwargs["message"]

    completed = AgentTaskOper().get(task.id)
    assert completed.enabled is False
    assert completed.last_status == "success"
    assert completed.last_result == "已找到 2 个资源"
    assert completed.run_count == 1

    recurring_task = AgentTaskOper().add(
        name="周期检查电影资源",
        content="继续检查示例电影是否已有资源",
        trigger_type="cron",
        cron_expression="*/30 * * * *",
        run_at=None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-123",
    )

    success, _ = await manager.execute_scheduled_task(recurring_task.id)

    assert success is True
    recurring_completed = AgentTaskOper().get(recurring_task.id)
    assert recurring_completed.enabled is True
    assert recurring_completed.last_status == "success"
    assert recurring_completed.run_count == 1


@pytest.mark.anyio
async def test_agent_manager_dispatches_contextless_result_to_admin(
        monkeypatch,
) -> None:
    """无原消息渠道的 MCP 任务应捕获结果并发送到管理员通知渠道。"""
    user_id = f"api-{uuid4().hex}"
    task = AgentTaskOper().add(
        name="后台检查资源",
        content="检查示例电影是否已有资源",
        trigger_type="cron",
        cron_expression="0 * * * *",
        run_at=None,
        user_id=user_id,
        username="API Client",
        session_id=f"session-{user_id}",
        channel=None,
        source="api",
        original_chat_id=None,
    )
    manager = AgentManager()
    manager.process_message = AsyncMock(return_value="后台检查完成")
    post_message = AsyncMock()
    monkeypatch.setattr(AgentChain, "async_post_message", post_message)

    success, result = await manager.execute_scheduled_task(task.id)

    assert success is True
    assert result == "后台检查完成"
    kwargs = manager.process_message.await_args.kwargs
    assert kwargs["reply_mode"] == ReplyMode.CAPTURE_ONLY
    assert kwargs["allow_message_tools"] is False
    notification = post_message.await_args.args[0]
    assert notification.userid is None
    assert notification.username == settings.SUPERUSER
    assert notification.text == "后台检查完成"
