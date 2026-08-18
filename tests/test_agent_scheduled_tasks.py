import asyncio
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from langchain_core.messages import AIMessage

from app.agent import (
    AgentChain,
    AgentManager,
    MoviePilotAgent,
    ReplyMode,
    _MessageTask,
)
from app.agent.middleware.tool_selection import ToolSelectorMiddleware
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.create_agent_task import (
    CreateAgentTaskInput,
    CreateAgentTaskTool,
)
from app.agent.tools.impl.delete_agent_task import DeleteAgentTaskTool
from app.agent.tools.impl.query_agent_tasks import QueryAgentTasksTool
from app.agent.tools.impl.query_schedulers import QuerySchedulersTool
from app.agent.tools.impl.run_agent_task import RunAgentTaskTool
from app.agent.tools.impl.run_scheduler import RunSchedulerTool
from app.agent.tools.impl.send_message import SendMessageTool
from app.agent.tools.impl.update_agent_task import (
    UpdateAgentTaskInput,
    UpdateAgentTaskTool,
)
from app.agent.tools.tags import ToolTag
from app.runtime.config import settings
from app.db import SessionFactory
from app.db.oper.agenttask import AgentTaskOper
from app.db.models.agenttask import AgentTask
from app.schemas import ScheduleInfo
from app.scheduler import Scheduler
from app.runtime.scheduling import TimerUtils


class _FakeAgentTaskScheduler:
    """记录 Agent 定时任务工具触发的运行时调度变更。"""

    def __init__(self) -> None:
        """初始化运行时调度记录。"""
        self.updated = []
        self.removed = []
        self.started = []

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

    def start_agent_task(self, task_id: int) -> bool:
        """记录 Agent 任务立即执行投递。"""
        self.started.append(task_id)
        return True


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


def _past_time(minutes: int = 10) -> str:
    """生成系统时区内的过去时间字符串。"""
    timezone = pytz.timezone(settings.TZ)
    return (datetime.now(timezone) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


def _invalid_time() -> str:
    """返回无法构造自动触发器的遗留时间值。"""
    return "invalid-run-at"


def _add_agent_task(trigger_type: str, trigger_value: str, prefix: str):
    """创建带隔离用户上下文的 Agent 定时任务。"""
    user_id = f"{prefix}-{uuid4().hex}"
    return AgentTaskOper().add(
        name=f"{prefix} 检查",
        content="检查资源并报告",
        trigger_type=trigger_type,
        cron_expression=trigger_value if trigger_type == "cron" else None,
        run_at=trigger_value if trigger_type == "date" else None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-1",
    )


def _build_agent_task_scheduler(reconcile: bool = False) -> Scheduler:
    """构造不启动后台线程的 Agent 任务调度器。"""
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._scheduler = BackgroundScheduler(timezone=settings.TZ)
    scheduler._agent_task_interruptions_reconciled = False
    if reconcile:
        scheduler._reconcile_agent_task_interruptions()
    return scheduler


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
        "run_agent_task",
        "delete_agent_task",
    }.issubset(tool_names)
    agent_task_tool_names = {
        "create_agent_task",
        "query_agent_tasks",
        "update_agent_task",
        "run_agent_task",
        "delete_agent_task",
    }
    assert {
        tool_class.model_fields["name"].default
        for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES
        if tool_class.model_fields["name"].default in agent_task_tool_names
    } == agent_task_tool_names
    assert "delay_minutes" in CreateAgentTaskInput.model_json_schema()["properties"]

    agent_task_tools = [
        _build_tool(tool_class, "admin-user")
        for tool_class in (
            CreateAgentTaskTool,
            QueryAgentTasksTool,
            UpdateAgentTaskTool,
            RunAgentTaskTool,
            DeleteAgentTaskTool,
        )
    ]
    assert all(ToolTag.AgentTask.value in tool.tags for tool in agent_task_tools)
    assert all(ToolTag.Scheduler.value not in tool.tags for tool in agent_task_tools)
    scheduler_tool = _build_tool(QuerySchedulersTool, "admin-user")
    assert ToolTag.Scheduler.value in scheduler_tool.tags
    assert ToolTag.AgentTask.value not in scheduler_tool.tags
    runtime_scheduler_tools = [
        scheduler_tool,
        _build_tool(RunSchedulerTool, "admin-user"),
    ]
    all_scheduler_tools = [*agent_task_tools, *runtime_scheduler_tools]
    tool_groups = dict(
        ToolSelectorMiddleware._build_tool_groups(
            available_tools=all_scheduler_tools,
            valid_tool_names=[tool.name for tool in all_scheduler_tools],
        )
    )
    assert tool_groups[ToolTag.AgentTask.value] == [
        "create_agent_task",
        "query_agent_tasks",
        "update_agent_task",
        "run_agent_task",
        "delete_agent_task",
    ]
    assert tool_groups[ToolTag.Scheduler.value] == [
        "query_schedulers",
        "run_scheduler",
    ]


def test_agent_prompt_declares_scheduler_tool_boundaries() -> None:
    """核心提示应明确自主任务与运行时调度服务的工具和 ID 边界。"""
    project_root = Path(__file__).resolve().parents[1]
    core_prompt = (project_root / "app/agent/prompt/System Core Prompt.txt").read_text(
        encoding="utf-8"
    )

    assert "Manage existing autonomous tasks with `query_agent_tasks`" in core_prompt
    assert "`run_agent_task`" in core_prompt
    assert "Use `query_schedulers` and `run_scheduler` only" in core_prompt
    assert "integer `task_id`" in core_prompt
    assert "string `job_id`" in core_prompt


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
    """Scheduler 应注册并完整列出 Agent 任务，供前端展示和动态移除。"""
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
    scheduler._scheduler.start(paused=True)
    try:
        scheduler_items = scheduler.list()
        agent_scheduler_item = next(
            item for item in scheduler_items if item.id == job_id
        )
        assert agent_scheduler_item.name == task.name
        assert agent_scheduler_item.provider == "[Agent]"

        scheduler.remove_agent_task_job(task.id)
        assert scheduler._scheduler.get_job(job_id) is None
        assert job_id not in scheduler._jobs
    finally:
        scheduler._scheduler.shutdown(wait=False)


@pytest.mark.parametrize(
    "run_time_factory",
    [_past_time, _future_time, _invalid_time],
)
def test_scheduler_restart_keeps_interrupted_date_task_manual_only(
        run_time_factory,
) -> None:
    """重启不得自动补跑结果未知的一次任务，但必须保留显式重跑入口。"""
    task = _add_agent_task("date", run_time_factory(), "restart-date")
    assert AgentTaskOper().mark_running(task.id)

    scheduler = _build_agent_task_scheduler(reconcile=True)
    scheduler.init_agent_task_jobs()
    scheduler.init_agent_task_jobs()

    recovered = AgentTaskOper().get(task.id)
    job_id = scheduler._get_agent_task_job_id(task.id)
    assert recovered.last_status == "interrupted"
    assert "可能已有部分操作" in recovered.last_result
    assert recovered.last_run_at is not None
    assert recovered.run_count == 0
    assert job_id in scheduler._jobs
    assert scheduler._scheduler.get_job(job_id) is None
    assert scheduler.get_agent_task_next_run(task.id) is None

    scheduler.start = Mock()
    assert scheduler.start_agent_task(task.id) is True
    scheduler.start.assert_called_once_with(
        job_id,
        task_id=task.id,
        trigger_source="manual",
    )


@pytest.mark.anyio
async def test_interrupted_date_task_manual_run_disables_and_removes_job(
        monkeypatch,
) -> None:
    """用户显式重跑中断的一次任务后，应按原有单次任务语义正常收口。"""
    task = _add_agent_task("date", _past_time(), "restart-date-manual")
    assert AgentTaskOper().mark_running(task.id)

    scheduler = _build_agent_task_scheduler(reconcile=True)
    scheduler.init_agent_task_jobs()

    process_message = AsyncMock(return_value="执行完成")
    manager = SimpleNamespace(
        execute_scheduled_task=AgentManager.execute_scheduled_task,
        process_message=process_message,
    )
    manager.execute_scheduled_task = AgentManager.execute_scheduled_task.__get__(manager)
    monkeypatch.setattr(
        "app.agent.runtime_loader.get_running_agent_manager",
        lambda: manager,
    )

    assert await scheduler.execute_agent_task(
        task.id,
        trigger_source="manual",
    ) == (True, "执行完成")
    process_message.assert_awaited_once()

    finished = AgentTaskOper().get(task.id)
    runs = AgentTaskOper().list_runs(task.id)
    job_id = scheduler._get_agent_task_job_id(task.id)
    assert finished.last_status == "success"
    assert finished.run_count == 1
    assert finished.enabled is False
    assert len(runs) == 2
    assert [run.trigger_source for run in runs] == ["manual", "scheduled"]
    assert job_id not in scheduler._jobs


@pytest.mark.anyio
async def test_scheduler_propagates_scheduled_trigger_source(monkeypatch) -> None:
    """自动调度入口应显式保持 scheduled 运行来源。"""
    task = _add_agent_task("cron", "0 * * * *", "scheduled-source")
    scheduler = _build_agent_task_scheduler()
    execute = AsyncMock(return_value=(True, "执行完成"))
    monkeypatch.setattr(
        "app.agent.runtime_loader.get_running_agent_manager",
        lambda: SimpleNamespace(execute_scheduled_task=execute),
    )

    assert await scheduler.execute_agent_task(task.id) == (True, "执行完成")
    execute.assert_awaited_once_with(task.id, trigger_source="scheduled")


@pytest.mark.parametrize("run_time_factory", [_future_time, _invalid_time])
@pytest.mark.anyio
async def test_interrupted_date_task_enable_toggle_stays_manual_only(
        monkeypatch,
        run_time_factory,
) -> None:
    """暂停或恢复中断的一次任务不得重新注册原自动触发。"""
    task = _add_agent_task("date", run_time_factory(), "interrupted-toggle")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    assert oper.mark_interrupted(task.id, "执行结果未知")

    scheduler = _build_agent_task_scheduler()
    scheduler.init_agent_task_jobs()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: scheduler,
    )
    tool = _build_tool(UpdateAgentTaskTool, task.user_id)

    paused = json.loads(await tool.run(task_id=task.id, enabled=False))
    assert paused["enabled"] is False
    assert paused["last_status"] == "interrupted"
    assert paused["next_run_at"] is None

    resumed = json.loads(await tool.run(task_id=task.id, enabled=True))
    job_id = scheduler._get_agent_task_job_id(task.id)
    assert resumed["enabled"] is True
    assert resumed["last_status"] == "interrupted"
    assert resumed["next_run_at"] is None
    assert job_id in scheduler._jobs
    assert scheduler._scheduler.get_job(job_id) is None


@pytest.mark.anyio
async def test_interrupted_date_task_new_trigger_rearms_schedule(monkeypatch) -> None:
    """用户明确重设触发时间时，可以清除中断状态并重新安排单次任务。"""
    task = _add_agent_task("date", _future_time(), "interrupted-reschedule")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    assert oper.mark_interrupted(task.id, "执行结果未知")

    scheduler = _build_agent_task_scheduler()
    scheduler.init_agent_task_jobs()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: scheduler,
    )
    updated = json.loads(
        await _build_tool(UpdateAgentTaskTool, task.user_id).run(
            task_id=task.id,
            trigger_type="date",
            delay_minutes=20,
            enabled=True,
        )
    )

    job_id = scheduler._get_agent_task_job_id(task.id)
    assert updated["last_status"] == "waiting"
    assert updated["last_result"] is None
    assert updated["next_run_at"] is not None
    assert scheduler._scheduler.get_job(job_id) is not None


@pytest.mark.anyio
async def test_interrupted_date_task_rejects_past_trigger_while_pausing(
        monkeypatch,
) -> None:
    """中断的一次任务即使同时暂停，也只能用新的未来时间离开中断状态。"""
    task = _add_agent_task("date", _future_time(), "interrupted-past-reschedule")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    assert oper.mark_interrupted(task.id, "执行结果未知")

    scheduler = _build_agent_task_scheduler()
    scheduler.init_agent_task_jobs()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: scheduler,
    )
    tool = _build_tool(UpdateAgentTaskTool, task.user_id)

    with pytest.raises(ValueError, match="必须晚于当前时间"):
        await tool.run(
            task_id=task.id,
            trigger_type="date",
            trigger=_past_time(),
            enabled=False,
        )

    unchanged = oper.get(task.id)
    assert unchanged.enabled is True
    assert unchanged.last_status == "interrupted"
    assert unchanged.last_result == "执行结果未知"


def test_interrupted_date_task_requires_explicit_reschedule_value() -> None:
    """仅重复 date 类型不构成重新排期，必须同时提供新的触发值。"""
    with pytest.raises(ValueError, match="必须提供 trigger 或 delay_minutes"):
        UpdateAgentTaskInput(task_id=1, trigger_type="date")


@pytest.mark.anyio
async def test_expired_date_task_rejects_enable_without_reschedule(
        monkeypatch,
) -> None:
    """普通单次任务恢复启用时仍需校验现有触发时间，避免过期补跑。"""
    task = _add_agent_task("date", _past_time(), "expired-enable")
    oper = AgentTaskOper()
    assert oper.update(task_id=task.id, payload={"enabled": False})

    scheduler = _build_agent_task_scheduler()
    scheduler.init_agent_task_jobs()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: scheduler,
    )

    with pytest.raises(ValueError, match="必须晚于当前时间"):
        await _build_tool(UpdateAgentTaskTool, task.user_id).run(
            task_id=task.id,
            enabled=True,
        )

    unchanged = oper.get(task.id)
    assert unchanged.enabled is False
    assert unchanged.last_status == "waiting"
    assert scheduler._get_agent_task_job_id(task.id) not in scheduler._jobs


def test_agent_task_interruption_transition_preserves_execution_evidence() -> None:
    """中断迁移只能消费 running 状态，并保留既有执行次数与开始时间。"""
    task = _add_agent_task("cron", "0 * * * *", "interrupt-state")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    running = oper.get(task.id)
    assert running.last_run_at is not None

    assert oper.mark_interrupted(task.id, "执行结果未知")
    assert not oper.mark_interrupted(task.id, "不得覆盖")

    interrupted = oper.get(task.id)
    assert interrupted.last_status == "interrupted"
    assert interrupted.last_result == "执行结果未知"
    assert interrupted.last_run_at == running.last_run_at
    assert interrupted.run_count == running.run_count == 0


def test_scheduler_restart_keeps_unstarted_date_task_misfire_behavior() -> None:
    """停机期间仅错过触发时间的一次任务仍应按原有语义补跑。"""
    task = _add_agent_task("date", _past_time(), "restart-waiting")

    scheduler = _build_agent_task_scheduler()

    scheduler.init_agent_task_jobs()

    recovered = AgentTaskOper().get(task.id)
    job_id = scheduler._get_agent_task_job_id(task.id)
    runtime_job = scheduler._scheduler.get_job(job_id)
    assert recovered.last_status == "waiting"
    assert runtime_job is not None
    assert runtime_job.misfire_grace_time is None


@pytest.mark.parametrize("success", [True, False])
def test_scheduler_restart_keeps_finished_date_task_misfire_behavior(
        success: bool,
) -> None:
    """已有单次任务终态仍沿用原调度语义，不被中断策略扩大影响。"""
    task = _add_agent_task("date", _past_time(), f"restart-finished-{success}")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    assert oper.finish(task.id, success=success, result="已收口")

    scheduler = _build_agent_task_scheduler()

    scheduler.init_agent_task_jobs()

    recovered = oper.get(task.id)
    runtime_job = scheduler._scheduler.get_job(
        scheduler._get_agent_task_job_id(task.id)
    )
    assert recovered.last_status == ("success" if success else "failed")
    assert runtime_job is not None
    assert runtime_job.misfire_grace_time is None


@pytest.mark.anyio
async def test_scheduler_config_reload_does_not_interrupt_running_agent_task() -> None:
    """同进程重建调度时不得把仍在执行的任务误判为进程中断。"""
    scheduler = _build_agent_task_scheduler(reconcile=True)

    task = _add_agent_task("cron", "0 * * * *", "reload-running")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)

    scheduler._reconcile_agent_task_interruptions()
    scheduler.init_agent_task_jobs()

    running = oper.get(task.id)
    assert running.last_status == "running"
    assert not oper.mark_running(task.id)

    manager = AgentManager()
    manager.process_message = AsyncMock()
    success, result = await manager.execute_scheduled_task(task.id)
    assert success is False
    assert result == "Agent 定时任务当前不可执行"
    manager.process_message.assert_not_awaited()


def test_scheduler_restart_keeps_interrupted_cron_future_schedule() -> None:
    """周期任务中断后只保留下次正常调度，不抹掉本轮中断事实。"""
    task = _add_agent_task("cron", "0 * * * *", "restart-cron")
    assert AgentTaskOper().mark_running(task.id)

    scheduler = _build_agent_task_scheduler(reconcile=True)
    scheduler.init_agent_task_jobs()
    scheduler.init_agent_task_jobs()

    recovered = AgentTaskOper().get(task.id)
    job_id = scheduler._get_agent_task_job_id(task.id)
    runtime_job = scheduler._scheduler.get_job(job_id)
    assert recovered.last_status == "interrupted"
    assert "可能已有部分操作" in recovered.last_result
    assert recovered.last_run_at is not None
    assert recovered.run_count == 0
    assert runtime_job is not None
    assert scheduler.get_agent_task_next_run(task.id) is not None

    scheduler.start = Mock()
    assert scheduler.start_agent_task(task.id) is True
    scheduler.start.assert_called_once_with(
        job_id,
        task_id=task.id,
        trigger_source="manual",
    )


def test_scheduler_restart_reconciles_disabled_running_agent_task() -> None:
    """停用状态不应掩盖上个进程遗留的运行中事实。"""
    task = _add_agent_task("cron", "0 * * * *", "restart-disabled")
    oper = AgentTaskOper()
    assert oper.mark_running(task.id)
    # 冷启动需兼容数据库中的异常状态组合；生产更新入口禁止修改运行中任务。
    with SessionFactory() as db:
        db.query(AgentTask).filter(AgentTask.id == task.id).update({"enabled": False})
        db.commit()

    scheduler = _build_agent_task_scheduler(reconcile=True)
    scheduler.init_agent_task_jobs()

    recovered = oper.get(task.id)
    assert recovered.last_status == "interrupted"
    assert recovered.enabled is False
    assert scheduler._get_agent_task_job_id(task.id) not in scheduler._jobs


def test_scheduler_starts_registered_agent_task_without_waiting() -> None:
    """Agent 任务立即执行入口应只调用运行时 start，并拒绝缺失或运行中的任务。"""
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {
        "agent-task-7": {
            "name": "测试 Agent 任务",
            "running": False,
        }
    }
    scheduler.start = Mock()

    assert scheduler.start_agent_task(7) is True
    scheduler.start.assert_called_once_with(
        "agent-task-7",
        task_id=7,
        trigger_source="manual",
    )

    scheduler._jobs["agent-task-7"]["running"] = True
    assert scheduler.start_agent_task(7) is False
    assert scheduler.start_agent_task(8) is False


@pytest.mark.anyio
async def test_dashboard_schedule_keeps_agent_tasks(monkeypatch) -> None:
    """前端后台服务接口必须保留 Agent 自主任务。"""
    from app.api.endpoints.dashboard import schedule

    scheduler_items = [
        ScheduleInfo(
            id="agent-task-7",
            name="检查资源",
            provider="[Agent]",
            status="等待",
            next_run="20 分钟后",
        )
    ]
    monkeypatch.setattr(
        "app.api.endpoints.dashboard.Scheduler",
        lambda: SimpleNamespace(list=lambda: scheduler_items),
    )

    result = await schedule(None)

    assert result == scheduler_items
    assert result[0].id == "agent-task-7"
    assert result[0].provider == "[Agent]"


@pytest.mark.anyio
async def test_scheduler_tools_exclude_agent_tasks(monkeypatch) -> None:
    """运行时调度查询应过滤 Agent 任务，并把两类查询边界写入工具描述。"""
    scheduler = SimpleNamespace(
        list=lambda: [
            ScheduleInfo(
                id="subscribe_search_all",
                name="订阅搜索",
                provider="[系统]",
                status="等待",
                next_run="10 分钟后",
            ),
            ScheduleInfo(
                id="agent-task-7",
                name="检查资源",
                provider="[Agent]",
                status="等待",
                next_run="20 分钟后",
            ),
        ]
    )
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: scheduler,
    )
    tool = _build_tool(QuerySchedulersTool, "admin-user")

    result = json.loads(await tool.run())

    assert [item["id"] for item in result] == ["subscribe_search_all"]
    assert tool.require_admin is True
    assert "excludes user-created autonomous agent tasks" in tool.description
    agent_query_tool = _build_tool(QueryAgentTasksTool, "admin-user")
    assert "owned by the current user" in agent_query_tool.description


@pytest.mark.anyio
async def test_run_scheduler_rejects_agent_task_job_id() -> None:
    """run_scheduler 不应接受 Agent 任务的运行时 job_id。"""
    tool = _build_tool(RunSchedulerTool, "admin-user")
    tool._run_scheduler_sync = Mock()

    result = await tool.run(job_id="agent-task-7")

    assert "run_agent_task" in result
    tool._run_scheduler_sync.assert_not_called()


@pytest.mark.anyio
async def test_agent_task_tools_manage_persistent_schedule(monkeypatch) -> None:
    """Agent 管理工具应完成创建、查询、暂停、修改和删除闭环。"""
    user_id = f"user-{uuid4().hex}"
    fake_scheduler = _FakeAgentTaskScheduler()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: fake_scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: fake_scheduler,
    )

    create_tool = _build_tool(CreateAgentTaskTool, user_id)
    created = json.loads(await create_tool.ainvoke({
        "name": "十分钟后检查",
        "content": "检查示例电影是否有资源，不要自动下载",
        "trigger_type": "date",
        "delay_minutes": 10,
    }))
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
    delayed_update = json.loads(await update_tool.ainvoke({
        "task_id": task_id,
        "trigger_type": "date",
        "delay_minutes": 20,
    }))
    assert delayed_update["trigger_type"] == "date"
    assert datetime.fromisoformat(delayed_update["run_at"]) > datetime.now(
        pytz.timezone(settings.TZ)
    )

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
    updated_jobs = list(fake_scheduler.updated)
    running_update = await update_tool.run(task_id=task_id, enabled=False)
    assert running_update == f"Agent 定时任务 {task_id} 正在执行，请稍后再修改"
    assert fake_scheduler.updated == updated_jobs
    AgentTaskOper().finish(task_id, success=True, result="完成")

    run_result = await _build_tool(RunAgentTaskTool, user_id).run(task_id=task_id)
    assert f"Agent 定时任务 {task_id} 已提交立即执行" in run_result
    assert fake_scheduler.started == [task_id]

    deleted = await _build_tool(DeleteAgentTaskTool, user_id).run(task_id=task_id)
    assert deleted == f"Agent 定时任务 {task_id} 已删除"
    assert fake_scheduler.removed == [task_id]


@pytest.mark.anyio
async def test_run_agent_task_enforces_owner_and_enabled_state(monkeypatch) -> None:
    """立即执行 Agent 任务时应校验当前用户归属和启用状态。"""
    owner_id = f"owner-{uuid4().hex}"
    task = AgentTaskOper().add(
        name="检查资源",
        content="检查资源并报告",
        trigger_type="cron",
        cron_expression="0 * * * *",
        run_at=None,
        user_id=owner_id,
        username="admin",
        session_id=f"session-{owner_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-1",
    )
    fake_scheduler = _FakeAgentTaskScheduler()
    monkeypatch.setattr("app.scheduler.Scheduler", lambda: fake_scheduler)
    monkeypatch.setattr(
        "app.application.scheduling._scheduler_class",
        lambda: fake_scheduler,
    )

    other_user_result = await _build_tool(
        RunAgentTaskTool,
        "another-user",
    ).run(task_id=task.id)
    assert "不存在或不属于当前用户" in other_user_result
    assert fake_scheduler.started == []

    AgentTaskOper().update(
        task_id=task.id,
        user_id=owner_id,
        payload={"enabled": False},
    )
    disabled_result = await _build_tool(
        RunAgentTaskTool,
        owner_id,
    ).run(task_id=task.id)
    assert "已暂停" in disabled_result
    assert fake_scheduler.started == []


@pytest.mark.anyio
async def test_agent_manager_executes_task_with_broadcast_delivery(
    monkeypatch,
) -> None:
    """定时触发应复用原 Agent 会话、广播结果并在单次执行后停用任务。"""
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
    post_message = AsyncMock()
    monkeypatch.setattr(AgentChain, "async_post_message", post_message)

    success, result = await manager.execute_scheduled_task(task.id)

    assert success is True
    assert result == "已找到 2 个资源"
    kwargs = manager.process_message.await_args.kwargs
    assert kwargs["session_id"] == task.session_id
    assert kwargs["user_id"] == user_id
    assert kwargs["channel"] is None
    assert kwargs["source"] is None
    assert kwargs["original_chat_id"] is None
    assert kwargs["reply_mode"] == ReplyMode.DISPATCH
    assert kwargs["allow_message_tools"] is True
    assert kwargs["wait_for_completion"] is True
    assert "搜索示例电影是否已有资源" in kwargs["message"]
    post_message.assert_not_awaited()

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
async def test_agent_manager_runs_contextless_task_in_broadcast_mode(
    monkeypatch,
) -> None:
    """无原消息渠道的任务也应由 Agent 广播结果且不由调度器重复补发。"""
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
    assert kwargs["channel"] is None
    assert kwargs["source"] is None
    assert kwargs["reply_mode"] == ReplyMode.DISPATCH
    assert kwargs["allow_message_tools"] is True
    post_message.assert_not_awaited()


@pytest.mark.anyio
async def test_agent_manager_records_cancelled_scheduled_task_as_failed() -> None:
    """被用户停止的定时 Agent 任务不得记录为成功完成。"""
    user_id = f"cancel-{uuid4().hex}"
    task = AgentTaskOper().add(
        name="取消中的后台检查",
        content="检查资源并报告",
        trigger_type="cron",
        cron_expression="0 * * * *",
        run_at=None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel=None,
        source="api",
        original_chat_id=None,
    )
    manager = AgentManager()
    manager.process_message = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await manager.execute_scheduled_task(task.id)

    completed = AgentTaskOper().get(task.id)
    assert completed.last_status == "failed"
    assert completed.last_result == "Agent 定时任务已取消"
    assert completed.run_count == 1


@pytest.mark.anyio
async def test_agent_manager_close_finishes_active_and_queued_scheduled_tasks() -> None:
    """正常关闭必须取消同会话中正在执行和排队的持久任务。"""
    user_id = f"shutdown-{uuid4().hex}"
    session_id = f"session-{user_id}"
    tasks = [
        AgentTaskOper().add(
            name=f"关闭中的后台检查 {index}",
            content="检查资源并报告",
            trigger_type="cron",
            cron_expression="0 * * * *",
            run_at=None,
            user_id=user_id,
            username="admin",
            session_id=session_id,
            channel=None,
            source="api",
            original_chat_id=None,
        )
        for index in range(2)
    ]
    manager = AgentManager()
    await manager.initialize()
    started = asyncio.Event()

    async def block_current_task(_task):
        started.set()
        await asyncio.Event().wait()

    manager._process_message_internal = block_current_task
    executions = [
        asyncio.create_task(manager.execute_scheduled_task(task.id))
        for task in tasks
    ]
    await asyncio.wait_for(started.wait(), timeout=1)
    for _ in range(50):
        if all(AgentTaskOper().get(task.id).last_status == "running" for task in tasks):
            break
        await asyncio.sleep(0)
    assert all(AgentTaskOper().get(task.id).last_status == "running" for task in tasks)

    await manager.close()
    results = await asyncio.gather(*executions, return_exceptions=True)

    assert all(
        result == (False, "Agent 定时任务执行失败：AgentManager 已关闭")
        for result in results
    )
    for task in tasks:
        completed = AgentTaskOper().get(task.id)
        assert completed.last_status == "failed"
        assert completed.last_result == "Agent 定时任务执行失败：AgentManager 已关闭"
        assert completed.run_count == 1


@pytest.mark.anyio
async def test_cached_agent_clears_channel_for_background_task() -> None:
    """复用会话 Agent 时，后台任务必须覆盖上一轮保留的渠道信息。"""
    manager = AgentManager()
    agent = MoviePilotAgent(
        session_id="scheduled-cached-session",
        user_id="user-1",
        channel="Telegram",
        source="telegram-test",
        username="admin",
        is_channel_admin=True,
        original_chat_id="chat-123",
    )
    agent.process = AsyncMock(return_value="完成")
    manager.active_agents[agent.session_id] = agent
    task = _MessageTask(
        session_id=agent.session_id,
        user_id="user-1",
        message="执行后台任务",
        channel=None,
        source=None,
        username="admin",
        original_chat_id=None,
        reply_mode=ReplyMode.DISPATCH,
        allow_message_tools=True,
    )

    result = await manager._process_message_internal(task)

    assert result == "完成"
    assert agent.channel is None
    assert agent.source is None
    assert agent.is_channel_admin is None
    assert agent.original_chat_id is None


@pytest.mark.anyio
async def test_cached_agent_overwrites_channel_admin_with_explicit_false() -> None:
    """复用会话 Agent 时，明确非管理员结论必须覆盖上一轮管理员身份。"""
    manager = AgentManager()
    agent = MoviePilotAgent(
        session_id="channel-admin-cached-session",
        user_id="user-1",
        channel="Telegram",
        source="telegram-test",
        username="admin",
        is_channel_admin=True,
    )
    agent.process = AsyncMock(return_value="完成")
    manager.active_agents[agent.session_id] = agent
    task = _MessageTask(
        session_id=agent.session_id,
        user_id="user-2",
        message="执行普通用户请求",
        channel="Telegram",
        source="telegram-test",
        username="admin",
        is_channel_admin=False,
    )

    result = await manager._process_message_internal(task)

    assert result == "完成"
    assert agent.user_id == "user-2"
    assert agent.username == "admin"
    assert agent.is_channel_admin is False


@pytest.mark.anyio
async def test_background_agent_final_message_is_broadcast() -> None:
    """后台 Agent 的最终消息应清空渠道及渠道用户定位后广播。"""
    agent = MoviePilotAgent(
        session_id="scheduled-broadcast-session",
        user_id="telegram-user-id",
        channel=None,
        source=None,
        username="admin",
        original_message_id="message-1",
        original_chat_id="chat-1",
    )

    with patch(
        "app.agent.orchestrator.AgentChain.async_post_message",
        new_callable=AsyncMock,
    ) as post_message:
        await agent.send_agent_message("任务完成", title="MoviePilot助手")

    notification = post_message.await_args.args[0]
    assert notification.channel is None
    assert notification.source is None
    assert notification.userid is None
    assert notification.original_message_id is None
    assert notification.original_chat_id is None
    assert notification.username == "admin"


@pytest.mark.anyio
async def test_background_send_message_tool_broadcasts() -> None:
    """后台 send_message 工具应广播消息并记录本轮已经回复。"""
    tool = SendMessageTool(
        session_id="scheduled-tool-session",
        user_id="telegram-user-id",
    )
    tool.set_message_attr(channel=None, source=None, username="admin")
    agent_context = {}
    tool.set_agent_context(agent_context)

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new_callable=AsyncMock,
    ) as post_message:
        result = await tool.run(message="工具已完成任务")

    assert result == "消息已发送"
    assert agent_context["user_reply_sent"] is True
    notification = post_message.await_args.args[0]
    assert notification.channel is None
    assert notification.source is None
    assert notification.userid is None
    assert notification.original_message_id is None
    assert notification.original_chat_id is None
    assert notification.username == "admin"


@pytest.mark.anyio
async def test_background_agent_does_not_repeat_tool_message() -> None:
    """消息工具已完成回复后，后台 Agent 不应再次发送最终文本。"""
    agent = MoviePilotAgent(
        session_id="scheduled-no-repeat-session",
        user_id="user-1",
        channel=None,
        source=None,
        username="admin",
        replay_mode=ReplyMode.DISPATCH,
    )
    agent._tool_context = {"user_reply_sent": True}
    agent._streamed_output = ""
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )
    agent._should_stream = lambda: False
    completed_agent = SimpleNamespace(
        ainvoke=AsyncMock(return_value=None),
        get_state=lambda _config: SimpleNamespace(
            values={"messages": [AIMessage(content="消息已发送")]}
        ),
    )
    agent._create_agent = AsyncMock(return_value=completed_agent)
    agent.send_agent_message = AsyncMock()

    await agent._execute_agent([])

    agent.send_agent_message.assert_not_awaited()
