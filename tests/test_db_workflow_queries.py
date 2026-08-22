"""
工作流表的查询与状态流转行为。

调度器按「触发类型 + 状态」取工作流，取多了会把用户暂停的流程重新跑起来，
取少了则定时任务永远不触发。状态流转里的 `state != 'P'` 守卫是暂停语义的唯一实现，
`run_count` 的自增必须留在 SQL 侧，否则并发执行会丢计数。
"""
import asyncio

import pytest

from app.db.models.workflow import Workflow
from app.db.session import async_session_scope


@pytest.fixture(autouse=True)
def _track(db):
    """把工作流表纳入用例级回收。"""
    db.watermark(Workflow)


def _flow(name: str, trigger_type: str = "timer", state: str = "W",
          run_count: int = 0) -> Workflow:
    """构造一条工作流记录。"""
    return Workflow(name=name, description=name, timer="0 * * * *",
                    trigger_type=trigger_type, state=state, run_count=run_count,
                    actions=[], flows=[], context={}, execution_state={})


async def _stage_async_action(workflow_id: int, action_id: str) -> None:
    """用独占异步会话提交一次模型级暂存，模拟 Application UoW 边界。"""
    async with async_session_scope() as session:
        await Workflow.async_update_current_action(
            session,
            wid=workflow_id,
            action_id=action_id,
            context={},
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# 列表查询
# --------------------------------------------------------------------------- #

def test_list_and_get_by_name_match_async_twins(db):
    """
    列举与按名查找的同步、异步结果必须一致。
    """
    created = db.add(_flow("wf-name"))

    assert Workflow.get_by_name(db.session, "wf-name").id == created.id
    assert asyncio.run(Workflow.async_get_by_name(name="wf-name")).id == created.id
    assert Workflow.get_by_name(db.session, "wf-missing") is None

    sync_ids = sorted(w.id for w in Workflow.list(db.session))
    async_ids = sorted(w.id for w in asyncio.run(Workflow.async_list()))
    assert sync_ids == async_ids


def test_enabled_workflows_exclude_paused(db):
    """
    启用列表排除暂停状态。

    暂停是用户显式的「别再跑了」，被列出即等于暂停开关失效。
    """
    db.add(_flow("wf-waiting", state="W"), _flow("wf-running", state="R"),
           _flow("wf-paused", state="P"))

    names = {w.name for w in Workflow.get_enabled_workflows(db.session)}

    assert {"wf-waiting", "wf-running"} <= names
    assert "wf-paused" not in names
    assert "wf-paused" not in {w.name for w in
                               asyncio.run(Workflow.async_get_enabled_workflows())}


def test_timer_triggered_includes_legacy_null_trigger_type(db):
    """
    定时触发列表要包含 trigger_type 为空的历史数据。

    该列是后加的，老工作流为空；严格等于 'timer' 会让它们从此再也不被调度，
    而用户看到的只是「任务不跑了」。
    """
    db.add(_flow("wf-timer", trigger_type="timer"),
           _flow("wf-legacy", trigger_type=None),
           _flow("wf-event", trigger_type="event"),
           _flow("wf-timer-paused", trigger_type="timer", state="P"))

    names = {w.name for w in Workflow.get_timer_triggered_workflows(db.session)}

    assert {"wf-timer", "wf-legacy"} <= names
    assert "wf-event" not in names
    assert "wf-timer-paused" not in names


def test_event_triggered_requires_explicit_type(db):
    """
    事件触发列表只认显式的 'event'，且同样排除暂停。
    """
    db.add(_flow("wf-event", trigger_type="event"),
           _flow("wf-event-paused", trigger_type="event", state="P"),
           _flow("wf-legacy", trigger_type=None))

    names = {w.name for w in Workflow.get_event_triggered_workflows(db.session)}

    assert names >= {"wf-event"}
    assert "wf-event-paused" not in names
    assert "wf-legacy" not in names


def test_trigger_lists_match_async_twins(db):
    """
    定时与事件两条触发列表的同步、异步结果必须一致。
    """
    db.add(_flow("wf-t", trigger_type="timer"), _flow("wf-e", trigger_type="event"))

    assert sorted(w.id for w in Workflow.get_timer_triggered_workflows(db.session)) == \
        sorted(w.id for w in asyncio.run(Workflow.async_get_timer_triggered_workflows()))
    assert sorted(w.id for w in Workflow.get_event_triggered_workflows(db.session)) == \
        sorted(w.id for w in asyncio.run(Workflow.async_get_event_triggered_workflows()))


# --------------------------------------------------------------------------- #
# 状态流转
# --------------------------------------------------------------------------- #

def test_update_state_and_start_write_the_state(db):
    """
    状态更新与启动直接落库，供调度器读到最新状态。
    """
    flow = db.add(_flow("wf-state"))

    Workflow.update_state(db.session, flow.id, "F")
    assert Workflow.get_by_name(db.session, "wf-state").state == "F"

    Workflow.start(db.session, flow.id)
    assert Workflow.get_by_name(db.session, "wf-state").state == "R"


def test_fail_and_success_respect_the_paused_guard(db):
    """
    暂停中的工作流不接受成功/失败结果写入。

    守卫丢失后，一个还在跑的旧任务收尾时会把用户刚设的暂停状态改掉，
    下一轮调度它又被跑起来。
    """
    paused = db.add(_flow("wf-paused", state="P"))

    Workflow.fail(db.session, paused.id, "出错了")
    assert Workflow.get_by_name(db.session, "wf-paused").state == "P"

    Workflow.success(db.session, paused.id, "完成")
    assert Workflow.get_by_name(db.session, "wf-paused").state == "P"


def test_fail_records_result_and_timestamp(db):
    """
    失败要同时写入结果与最后执行时间，供界面展示失败原因。
    """
    flow = db.add(_flow("wf-fail", state="R"))

    Workflow.fail(db.session, flow.id, "网络超时")

    updated = Workflow.get_by_name(db.session, "wf-fail")
    assert (updated.state, updated.result) == ("F", "网络超时")
    assert updated.last_time


def test_success_increments_run_count_in_sql(db):
    """
    执行次数必须在 SQL 侧自增。

    先读后写会在并发执行时丢计数；连续两次成功后必须是 2。
    """
    flow = db.add(_flow("wf-count", state="R", run_count=0))

    Workflow.success(db.session, flow.id, "第一次")
    Workflow.success(db.session, flow.id, "第二次")

    updated = Workflow.get_by_name(db.session, "wf-count")
    assert updated.run_count == 2
    assert updated.state == "S"


def test_reset_clears_progress_and_optionally_the_count(db):
    """
    重置清空执行进度；是否清零执行次数由参数决定。

    默认保留计数是为了让「重跑」不丢失历史执行统计。
    """
    flow = db.add(_flow("wf-reset", state="F", run_count=5))
    Workflow.update_current_action(db.session, flow.id, "action-1",
                                   {"k": "v"}, {"step": 1})

    Workflow.reset(db.session, flow.id)
    kept = Workflow.get_by_name(db.session, "wf-reset")
    assert (kept.state, kept.result, kept.current_action) == ("W", None, None)
    assert kept.context == {} and kept.execution_state == {}
    assert kept.run_count == 5

    Workflow.reset(db.session, flow.id, reset_count=True)
    assert Workflow.get_by_name(db.session, "wf-reset").run_count == 0


def test_update_current_action_appends_without_duplicating(db):
    """
    已执行动作按逗号追加且不重复登记。

    重复登记会让「已执行」列表无限膨胀，重跑时的跳过判断也随之失准。
    """
    flow = db.add(_flow("wf-action"))

    Workflow.update_current_action(db.session, flow.id, "a1", {"n": 1})
    Workflow.update_current_action(db.session, flow.id, "a2", {"n": 2})
    Workflow.update_current_action(db.session, flow.id, "a1", {"n": 3})

    updated = Workflow.get_by_name(db.session, "wf-action")
    assert updated.current_action == "a1,a2"
    assert updated.context == {"n": 3}


def test_update_current_action_leaves_execution_state_untouched_when_omitted(db):
    """
    不传执行状态时保持原值，避免一次进度更新把结构化状态清空。
    """
    flow = db.add(_flow("wf-keep-state"))
    Workflow.update_current_action(db.session, flow.id, "a1", {}, {"step": 7})

    Workflow.update_current_action(db.session, flow.id, "a2", {"n": 1})

    assert Workflow.get_by_name(db.session, "wf-keep-state").execution_state == {"step": 7}


def test_update_current_action_matches_async_twin(db):
    """
    同步与异步的动作追加必须给出相同的字符串。
    """
    sync_flow = db.add(_flow("wf-sync-action"))
    async_flow = db.add(_flow("wf-async-action"))

    for action in ("a1", "a2", "a1"):
        Workflow.update_current_action(db.session, sync_flow.id, action, {})
        # 同步 Model 方法只暂存 SQL；由测试持有的事务边界先提交，避免与异步会话争锁。
        db.session.commit()
        asyncio.run(_stage_async_action(async_flow.id, action))

    assert Workflow.get_by_name(db.session, "wf-sync-action").current_action == \
        Workflow.get_by_name(db.session, "wf-async-action").current_action
