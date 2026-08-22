import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, current_thread
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.agent.orchestrator import AgentManager
from app.agent.tools.impl.query_agent_tasks import QueryAgentTasksTool
from app.db.engine import get_engine
from app.db.oper.agenttask import AgentTaskOper
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun
from app.db.session import SessionFactory


Engine = get_engine()


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。

    受测的阻塞查询经 ``run_agent_blocking`` 调用 ``asyncio.get_running_loop``，
    在 trio 后端下没有 running asyncio loop，必然以
    ``RuntimeError: no running event loop`` 失败；与业务逻辑无关，故不参数化到 trio。
    """
    return "asyncio"


def _add_task(prefix: str, *, trigger_type: str = "cron") -> AgentTask:
    """创建带隔离 owner 的 Agent 自主任务。"""
    user_id = f"{prefix}-{uuid4().hex}"
    return AgentTaskOper().add(
        name=f"{prefix} 检查",
        content="检查资源并报告",
        trigger_type=trigger_type,
        cron_expression="0 * * * *" if trigger_type == "cron" else None,
        run_at="2099-01-01T00:00:00+08:00" if trigger_type == "date" else None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel="Telegram",
        source="telegram-test",
        original_chat_id="chat-1",
    )


def _build_query_tool(user_id: str) -> QueryAgentTasksTool:
    """构造绑定当前 owner 的任务查询工具。"""
    tool = QueryAgentTasksTool(session_id=f"session-{user_id}", user_id=user_id)
    tool._message_context = {"username": "admin"}
    return tool


def test_begin_run_claims_once_and_preserves_snapshot() -> None:
    """并发认领只能创建一个 run，且任务修改不改变执行快照。"""
    task = _add_task("run-claim")

    with ThreadPoolExecutor(max_workers=2) as executor:
        runs = list(executor.map(
            lambda source: AgentTaskOper().begin_run(task.id, source),
            ("scheduled", "manual"),
        ))

    created = [run for run in runs if run]
    assert len(created) == 1
    run = created[0]
    assert run.trigger_source in {"scheduled", "manual"}
    assert run.name == task.name
    assert run.content == task.content
    assert not AgentTaskOper().update(task.id, {"name": "运行中不可修改"})
    assert AgentTaskOper().finish_run(run.run_id, success=True, result="完成")
    assert AgentTaskOper().update(task.id, {"name": "新名称", "content": "新内容"})

    snapshot = AgentTaskOper().get_run(run.run_id)
    current = AgentTaskOper().get(task.id)
    assert snapshot.name == task.name
    assert snapshot.content == task.content
    assert current.name == "新名称"
    assert current.content == "新内容"
    assert current.last_run_id == run.run_id


def test_begin_run_uses_configuration_committed_before_atomic_claim(monkeypatch) -> None:
    """配置先完成写入时，执行快照不得因秒级时间相同而读取旧值。"""
    fixed_time = "2026-08-13 20:00:00"
    monkeypatch.setattr(AgentTaskOper, "_now", staticmethod(lambda: fixed_time))
    task = _add_task("run-current-snapshot")
    claim_ready = Event()
    update_done = Event()
    result = {}

    def pause_before_claim(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
    ) -> None:
        if (
                current_thread().name == "agent-task-claim"
                and statement.lstrip().upper().startswith("UPDATE AGENTTASK SET")
        ):
            claim_ready.set()
            assert update_done.wait(timeout=5)

    def begin() -> None:
        result["run"] = AgentTaskOper().begin_run(task.id)

    event.listen(Engine, "before_cursor_execute", pause_before_claim)
    try:
        thread = Thread(target=begin, name="agent-task-claim")
        thread.start()
        assert claim_ready.wait(timeout=5)
        assert AgentTaskOper().update(
            task.id,
            {"name": "最新名称", "content": "最新内容"},
        )
        update_done.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        update_done.set()
        event.remove(Engine, "before_cursor_execute", pause_before_claim)

    run = result["run"]
    assert run.name == "最新名称"
    assert run.content == "最新内容"


def test_begin_run_rejects_unknown_trigger_source() -> None:
    """运行记录只接受已定义的定时或手动触发入口。"""
    task = _add_task("run-source")

    with pytest.raises(ValueError, match="不支持的 Agent 任务触发来源"):
        AgentTaskOper().begin_run(task.id, "retry")

    unchanged = AgentTaskOper().get(task.id)
    assert unchanged.last_status == "waiting"
    assert unchanged.last_run_id is None
    assert AgentTaskOper().list_runs(task.id) == []


def test_begin_run_rolls_back_task_claim_when_run_insert_fails() -> None:
    """运行记录插入失败时，任务的 running 投影必须随事务回滚。"""
    first_task = _add_task("run-rollback-first")
    second_task = _add_task("run-rollback-second")
    run_id = uuid4().hex
    assert AgentTaskOper().begin_run(
        task_id=first_task.id,
        run_id=run_id,
        trigger_source="scheduled",
        started_at="2026-08-13 20:00:00",
    ).run_id == run_id

    with pytest.raises(IntegrityError):
        AgentTaskOper().begin_run(
            task_id=second_task.id,
            run_id=run_id,
            trigger_source="manual",
            started_at="2026-08-13 20:00:01",
        )

    unchanged = AgentTaskOper().get(second_task.id)
    assert unchanged.last_status == "waiting"
    assert unchanged.last_run_id is None
    assert len(AgentTaskOper().list_runs(first_task.id)) == 1
    assert AgentTaskOper().list_runs(second_task.id) == []


def test_finish_run_finalizes_once_under_concurrency() -> None:
    """同一 run 的并发收口只能有一个成功并只累计一次。"""
    task = _add_task("run-finish-once")
    run = AgentTaskOper().begin_run(task.id)
    assert run

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda value: AgentTaskOper().finish_run(
                run.run_id,
                success=True,
                result=value,
            ),
            ("结果 A", "结果 B"),
        ))

    assert sorted(results) == [False, True]
    completed = AgentTaskOper().get(task.id)
    finalized = AgentTaskOper().get_run(run.run_id)
    assert completed.last_status == "success"
    assert completed.last_result in {"结果 A", "结果 B"}
    assert completed.run_count == 1
    assert finalized.status == "success"
    assert finalized.result == completed.last_result


def test_stale_finish_cannot_overwrite_latest_run_projection() -> None:
    """迟到的旧运行只能收口自己，不得覆盖任务的最新运行投影。"""
    task = _add_task("run-stale")
    oper = AgentTaskOper()
    first = oper.begin_run(task.id, "scheduled")
    assert first

    with SessionFactory() as db:
        db.query(AgentTask).filter(AgentTask.id == task.id).update({
            "last_status": "interrupted",
        })
        db.commit()
    second = oper.begin_run(task.id, "manual")
    assert second

    assert oper.finish_run(first.run_id, success=True, result="旧结果")
    current = oper.get(task.id)
    assert current.last_run_id == second.run_id
    assert current.last_status == "running"
    assert current.last_result is None
    assert current.run_count == 0
    assert oper.get_run(first.run_id).status == "success"

    assert oper.finish_run(second.run_id, success=False, result="新结果")
    finished = oper.get(task.id)
    assert finished.last_status == "failed"
    assert finished.last_result == "新结果"
    assert finished.run_count == 1


def test_interruption_requires_matching_running_run() -> None:
    """有 run 指针时，对账不得只改任务而留下不一致的运行历史。"""
    task = _add_task("run-interrupt-mismatch")
    oper = AgentTaskOper()
    run = oper.begin_run(task.id)
    assert run
    with SessionFactory() as db:
        db.query(AgentTaskRun).filter(AgentTaskRun.run_id == run.run_id).update({
            "status": "success",
            "result": "已收口",
        })
        db.commit()

    assert not oper.mark_interrupted(task.id, "不得覆盖")
    unchanged = oper.get(task.id)
    assert unchanged.last_status == "running"
    assert unchanged.last_result is None
    assert oper.get_run(run.run_id).status == "success"


def test_interruption_supports_legacy_running_task_without_run() -> None:
    """升级前遗留的 running 投影没有 run 指针时仍需兼容对账。"""
    task = _add_task("run-interrupt-legacy")
    with SessionFactory() as db:
        db.query(AgentTask).filter(AgentTask.id == task.id).update({
            "last_status": "running",
            "last_run_id": None,
        })
        db.commit()

    oper = AgentTaskOper()
    assert oper.mark_interrupted(task.id, "旧任务结果未知")
    interrupted = oper.get(task.id)
    assert interrupted.last_status == "interrupted"
    assert interrupted.last_result == "旧任务结果未知"
    assert interrupted.last_run_id is None
    assert oper.list_runs(task.id) == []


def test_interruption_and_manual_rerun_keep_distinct_history() -> None:
    """中断对账与显式重跑应保留两条互不覆盖的执行记录。"""
    task = _add_task("run-interrupt", trigger_type="date")
    oper = AgentTaskOper()
    first = oper.begin_run(task.id, "scheduled")
    assert first
    assert oper.mark_interrupted(task.id, "执行结果未知")
    assert oper.get_run(first.run_id).status == "interrupted"

    second = oper.begin_run(task.id, "manual")
    assert second and second.run_id != first.run_id
    assert oper.finish_run(
        second.run_id,
        success=True,
        result="重跑完成",
        disable_date_task=True,
    )

    runs = oper.list_runs(task.id)
    assert [run.run_id for run in runs] == [second.run_id, first.run_id]
    assert [run.status for run in runs] == ["success", "interrupted"]
    finished = oper.get(task.id)
    assert finished.enabled is False
    assert finished.last_status == "success"
    assert finished.run_count == 1


def test_delete_rejects_running_task_and_removes_all_history() -> None:
    """运行中任务不可删除，收口后永久删除不得留下孤立 run。"""
    task = _add_task("run-delete")
    oper = AgentTaskOper()
    run = oper.begin_run(task.id)
    assert run
    assert not oper.delete(task.id, user_id=task.user_id)
    assert oper.get(task.id) is not None
    assert oper.finish_run(run.run_id, success=True, result="完成")

    assert not oper.delete(task.id, user_id="other-user")
    assert oper.delete(task.id, user_id=task.user_id)
    assert oper.get(task.id) is None
    assert oper.get_run(run.run_id) is None


@pytest.mark.anyio
async def test_query_task_returns_owner_scoped_ten_recent_runs(monkeypatch) -> None:
    """单任务查询只向 owner 返回最近十次运行，列表查询不携带历史。"""
    task = _add_task("run-query")
    other = _add_task("run-query-other")
    oper = AgentTaskOper()
    expected = []
    for index in range(12):
        run = oper.begin_run(task.id, "manual" if index % 2 else "scheduled")
        assert run
        assert oper.finish_run(run.run_id, success=True, result=f"结果 {index}")
        expected.insert(0, run.run_id)
    other_run = oper.begin_run(other.id)
    assert other_run
    assert oper.finish_run(other_run.run_id, success=True, result="其他用户")

    monkeypatch.setattr(
        "app.application.scheduling.get_agent_task_next_run",
        lambda _task_id: None,
    )
    detail = json.loads(await _build_query_tool(task.user_id).run(task_id=task.id))
    assert detail["total"] == 1
    assert [run["run_id"] for run in detail["tasks"][0]["recent_runs"]] == expected[:10]
    assert all(run["task_id"] == task.id for run in detail["tasks"][0]["recent_runs"])

    listing = json.loads(await _build_query_tool(task.user_id).run())
    assert listing["total"] == 1
    assert "recent_runs" not in listing["tasks"][0]
    hidden = json.loads(await _build_query_tool(other.user_id).run(task_id=task.id))
    assert hidden == {"total": 0, "tasks": []}


@pytest.mark.anyio
async def test_agent_manager_records_manual_trigger_source(monkeypatch) -> None:
    """真实执行入口应把手动触发来源写入对应 run。"""
    monkeypatch.setattr("app.agent.orchestrator.settings.AI_AGENT_ENABLE", True)
    task = _add_task("run-manager")
    manager = AgentManager()
    captured = {}

    async def process_message(**kwargs):
        captured.update(kwargs)
        return "完成"

    manager.process_message = process_message
    assert await manager.execute_scheduled_task(task.id, trigger_source="manual") == (
        True,
        "完成",
    )
    runs = AgentTaskOper().list_runs(task.id)
    assert len(runs) == 1
    assert runs[0].trigger_source == "manual"
    assert runs[0].status == "success"
    assert "定时任务已手动触发" in captured["message"]
