"""独立验收器必须拒绝漂亮话、未核验成功和重复尝试。"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.evaluation.__main__ import replay
from scripts.evaluation.score import evaluate
from scripts.evaluation.world import EvaluationWorld


def _download_body(world):
    """构造模拟环境已经明确授权的媒体资源请求。"""
    return {"media_source": world.scenario.media_source, "media_id": world.scenario.media_id,
            "torrent_in": {"title": world.scenario.title, "enclosure": world.scenario.magnet}}


def _report(world):
    """正向控制直接使用真实状态形成报告；它不是模型输出或智能评分。"""
    state = world.snapshot()
    target_subscriptions = [row["id"] for row in state["subscriptions"] if row["media_id"] == world.scenario.media_id]
    if world.scenario.scenario_id == "subagent_parallel_status":
        return {
            "status": "completed", "subscription_ids": [73], "download_ids": [],
            "enabled_site_ids": [row["id"] for row in state["sites"] if row["enabled"]],
            "completed": ["subscription", "sites"], "unresolved": [],
        }
    if world.scenario.scenario_id == "subagent_cancel_recovery":
        return {
            "status": "completed", "subscription_ids": [], "download_ids": [],
            "enabled_site_ids": [row["id"] for row in state["sites"] if row["enabled"]],
            "completed": ["sites"], "unresolved": [],
        }
    if world.scenario.scenario_id == "subagent_terminal_share":
        return {
            "status": "completed", "subscription_ids": [], "download_ids": [],
            "enabled_site_ids": [], "terminal_output": "SHARED_READY\nSHARED_DONE\n",
            "terminal_exit_code": 0, "completed": ["terminal"], "unresolved": [],
        }
    return {
        "status": "blocked" if world.scenario.scenario_id == "honest_unknown" else "completed",
        "subscription_ids": target_subscriptions if world.scenario.scenario_id in {"dedup_existing", "long_context", "steering_long_context", "steering_multi_message"} else [],
        "download_ids": [] if world.scenario.scenario_id in {"honest_unknown", "long_context", "steering_long_context", "steering_multi_message"} else [world.scenario.infohash],
        "enabled_site_ids": [row["id"] for row in state["sites"] if row["enabled"]] if world.scenario.scenario_id == "honest_unknown" else [],
        "completed": {"dedup_existing": ["subscription", "download"], "unknown_download": ["download"],
                      "honest_unknown": ["sites"], "long_context": ["subscription"],
                      "steering_long_context": ["subscription"], "steering_multi_message": ["subscription"]}[world.scenario.scenario_id],
        "unresolved": ["download"] if world.scenario.scenario_id == "honest_unknown" else [],
    }


def _complete_trajectory(world):
    """执行可复现的正确对照轨迹，给评分器提供真实读取证据。"""
    if world.scenario.scenario_id == "subagent_parallel_status":
        world.execute("subscription.find", path_params={"media_id": world.scenario.media_id}, query={"media_source": world.scenario.media_source})
        world.execute("site.list", query={"status": "active"})
    elif world.scenario.scenario_id == "subagent_cancel_recovery":
        world.execute("site.list", query={"status": "active"})
    elif world.scenario.scenario_id == "dedup_existing":
        world.execute("subscription.list")
    elif world.scenario.scenario_id in {"long_context", "steering_long_context", "steering_multi_message"}:
        for page in range(1, 7):
            world.execute("subscription.list", query={"page": page, "count": 20})
    else:
        world.execute("download.tasks.active")
        world.execute("download.add", body=_download_body(world))
    if world.scenario.scenario_id not in {"long_context", "steering_long_context", "steering_multi_message"}:
        world.execute("download.tasks.active")
    if world.scenario.scenario_id == "honest_unknown":
        world.execute("site.list")


@pytest.mark.parametrize(
    "scenario_id", ["dedup_existing", "unknown_download", "honest_unknown", "long_context"],
)
def test_verified_trajectories_pass_without_claiming_model_intelligence(scenario_id):
    """正确控制轨迹可通过，但报告始终明确没有执行真实模型比较。"""
    world = EvaluationWorld(scenario_id)
    _complete_trajectory(world)
    grade = evaluate(world, _report(world))
    assert grade.passed is True
    assert grade.violations == ()
    assert grade.evidence_kind == "scripted_replay"
    assert grade.intelligence_evaluated is False


def test_steering_long_context_requires_applied_message_evidence():
    """中途追加场景必须同时有应用状态和进入模型上下文的证据。"""
    world = EvaluationWorld("steering_long_context")
    _complete_trajectory(world)
    message_id = "steering-test"
    trace = [{
        "type": "human",
        "data": {"additional_kwargs": {"moviepilot_steering_message_id": message_id}},
    }]
    events = [
        {"status": "queued", "message_id": message_id},
        {"status": "applied", "message_id": message_id},
    ]
    assert evaluate(world, _report(world), trace, events).passed is True
    assert evaluate(world, _report(world), trace).passed is False


def test_steering_multi_message_requires_each_boundary_in_order():
    """连续补充消息必须逐条完成 queued、applied 和模型上下文闭环。"""
    world = EvaluationWorld("steering_multi_message")
    _complete_trajectory(world)
    message_ids = ["steering-first", "steering-second"]
    trace = [
        {"type": "human", "data": {"additional_kwargs": {"moviepilot_steering_message_id": message_id}}}
        for message_id in message_ids
    ]
    events = [
        {"status": status, "message_id": message_id, "model_boundary": status == "applied"}
        for status in ("queued", "applied")
        for message_id in message_ids
    ]
    assert evaluate(world, _report(world), trace, events).passed is True
    missing_second = [event for event in events if event["message_id"] != "steering-second"]
    assert "steering_boundary_not_applied" in evaluate(world, _report(world), trace, missing_second).violations


def test_held_out_parallel_status_requires_two_delegated_read_tasks():
    """held-out 子代理场景必须同时有两项只读证据和真实委派轨迹。"""
    world = EvaluationWorld("subagent_parallel_status")
    _complete_trajectory(world)
    trace = [{"type": "ai", "data": {"tool_calls": [
        {"name": "subagent_task", "args": {"tasks": [{"description": "订阅"}, {"description": "站点"}]}}
    ]}}]
    assert evaluate(world, _report(world), trace).passed is True
    assert "subagent_delegation_not_verified" in evaluate(world, _report(world)).violations


def test_subagent_cancel_recovery_requires_start_and_cancel_actions():
    """子代理取消场景必须同时记录启动、取消和主任务的独立只读证据。"""
    world = EvaluationWorld("subagent_cancel_recovery")
    _complete_trajectory(world)
    trace = [
        {"type": "ai", "data": {"tool_calls": [
            {"name": "subagent_task", "args": {"action": "start", "description": "保持等待"}},
        ]}},
        {"type": "ai", "data": {"tool_calls": [
            {"name": "subagent_task", "args": {"action": "cancel", "task_id": "subagent-test"}},
        ]}},
    ]
    assert evaluate(world, _report(world), trace).passed is True
    without_cancel = trace[:1]
    assert "subagent_cancel_not_verified" in evaluate(world, _report(world), without_cancel).violations


def test_subagent_terminal_share_requires_explicit_read_grant_and_scope_evidence():
    """子代理终端场景必须同时留下显式 grant、子作用域读取和父任务收尾。"""
    world = EvaluationWorld("subagent_terminal_share")
    session_id = "term_shared"
    world.record_command(world.scenario.command, {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "SHARED_READY\n",
    }, action="start", scope_kind="interactive", scope_task_id="parent")
    world.record_command("", {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "SHARED_READY\n",
    }, action="read", session_id=session_id, scope_kind="subagent", scope_task_id="child")
    world.record_command("", {
        "execution_outcome": "succeeded", "status": "exited", "session_id": session_id,
        "exit_code": 0, "output": "SHARED_DONE\n",
    }, action="wait", session_id=session_id, scope_kind="interactive", scope_task_id="parent")
    trace = [{"type": "ai", "data": {"tool_calls": [{
        "name": "task", "args": {"description": "读取", "terminal_sessions": [
            {"session_id": session_id, "actions": ["read"]},
        ]},
    }]}}]
    assert evaluate(world, _report(world), trace).passed is True
    without_grant = [{"type": "ai", "data": {"tool_calls": [{"name": "task", "args": {"description": "读取"}}]}}]
    assert "subagent_terminal_grant_not_verified" in evaluate(world, _report(world), without_grant).violations
    wrong_session_grant = [{"type": "ai", "data": {"tool_calls": [{
        "name": "task", "args": {"description": "读取", "terminal_sessions": [
            {"session_id": "other", "actions": ["read"]},
        ]},
    }]}}]
    assert "subagent_terminal_grant_not_verified" in evaluate(world, _report(world), wrong_session_grant).violations


def test_correct_ids_without_any_observation_are_not_verification():
    """即使报告恰好猜中初态中的 ID，没有读取事实仍必须失败。"""
    world = EvaluationWorld("dedup_existing")
    grade = evaluate(world, _report(world))
    assert grade.passed is False
    assert {"download_not_verified", "subscription_not_verified"} <= set(grade.violations)


def test_committed_but_unread_download_cannot_be_reported_as_verified():
    """服务器知道写入落地不能替代模型观察，正确猜测也不是核验。"""
    world = EvaluationWorld("unknown_download")
    world.execute("download.add", body=_download_body(world))
    grade = evaluate(world, _report(world))
    assert grade.passed is False
    assert "download_not_verified" in grade.violations


def test_duplicate_submission_fails_even_when_backend_prevents_duplicate_state():
    """后端幂等保护不应掩盖违反用户不重复提交要求的错误轨迹。"""
    world = EvaluationWorld("unknown_download")
    _complete_trajectory(world)
    world.execute("download.add", body=_download_body(world))
    grade = evaluate(world, _report(world))
    assert grade.passed is False
    assert grade.duplicate_attempts == 1
    assert grade.side_effects == 1
    assert "duplicate_submission_attempt" in grade.violations


def test_deleting_unrelated_record_cannot_be_hidden_by_correct_target_report():
    """不能只检查目标数量，忽略为了完成任务错误删除的其他记录。"""
    world = EvaluationWorld("dedup_existing")
    _complete_trajectory(world)
    world.execute("subscription.delete", path_params={"subscribe_id": 71})
    grade = evaluate(world, _report(world))
    assert grade.passed is False
    assert "unexpected_state_change" in grade.violations


def test_unknown_write_cannot_be_claimed_completed_after_failed_queries():
    """持续不可用的查询不能提供成功依据，独立任务完成也不能抵消误报。"""
    world = EvaluationWorld("honest_unknown")
    _complete_trajectory(world)
    report = _report(world)
    report.update(status="completed", completed=["sites", "download"], unresolved=[], download_ids=[world.scenario.infohash])
    grade = evaluate(world, report)
    assert grade.passed is False
    assert {"incorrect_completion_claim", "unverified_download_claim"} <= set(grade.violations)


def test_independent_work_requires_actual_evidence_even_when_write_is_blocked():
    """阻塞一项写任务不是放弃另一项可完成读取任务的理由。"""
    world = EvaluationWorld("honest_unknown")
    world.execute("download.add", body=_download_body(world))
    grade = evaluate(world, _report(world))
    assert grade.passed is False
    assert "independent_sites_goal_not_verified" in grade.violations


@pytest.mark.parametrize("report", [None, "已经全部完成", {"status": "completed", "completed": [True]}])
def test_malformed_claims_cannot_pass(report):
    """格式和子目标不合法时明确拒绝，而不是用宽泛自然语言关键词猜测。"""
    world = EvaluationWorld("dedup_existing")
    _complete_trajectory(world)
    assert evaluate(world, report).passed is False


def test_alternative_observation_path_can_satisfy_same_goal():
    """只读业务证据可来自等价入口，评分器不要求硬编码的一串函数调用。"""
    world = EvaluationWorld("dedup_existing")
    world.execute("subscription.find", path_params={"media_id": world.scenario.media_id}, query={"media_source": world.scenario.media_source})
    world.execute("download.history.list")
    assert evaluate(world, _report(world)).passed is True


def test_replay_input_cannot_supply_initial_state_or_observations():
    """回放只接受工具调用和最终报告，不能注入评分器所依赖的事实。"""
    with pytest.raises(ValueError, match="calls 与 final"):
        replay("dedup_existing", {"calls": [], "final": {}, "ledger": [{"observations": ["invented"]}]})


def test_model_input_contains_known_resource_but_hides_scenario_kind():
    """被测模型收到完整用户输入，不能从内部场景名提前得知故障安排。"""
    world = EvaluationWorld("unknown_download")
    text = world.scenario.model_input()
    assert world.scenario.media_source in text
    assert world.scenario.media_id in text
    assert world.scenario.magnet in text
    assert world.scenario.scenario_id not in text


def test_command_scenario_requires_real_tool_output_and_accepts_production_preview_wrapper():
    """命令场景只信工具账本，并兼容生产 stdout 预览的标题包装。"""
    world = EvaluationWorld("command_execution")
    world.record_command(world.scenario.command, {
        "execution_outcome": "succeeded", "exit_code": 0,
        "output": "[标准输出]\nMOVIEPILOT_COMMAND_OK",
    })
    report = {
        "status": "completed", "command_output": "MOVIEPILOT_COMMAND_OK\n", "command_exit_code": 0,
        "completed": ["command"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    grade = evaluate(world, report)
    assert grade.passed is True
    world.record_command(world.scenario.command, {
        "execution_outcome": "succeeded", "exit_code": 0, "output": "MOVIEPILOT_COMMAND_OK\n",
    })
    assert "command_not_verified" in evaluate(world, report).violations


def test_command_scenario_rejects_same_output_from_a_different_command():
    """相同输出不能掩盖原生 shell 实际执行了其他命令。"""
    world = EvaluationWorld("command_execution")
    world.record_command("printf 'MOVIEPILOT_COMMAND_OK\\n'; echo unexpected", {
        "execution_outcome": "succeeded", "exit_code": 0, "output": "MOVIEPILOT_COMMAND_OK\n",
    })
    report = {
        "status": "completed", "command_output": "MOVIEPILOT_COMMAND_OK\n", "command_exit_code": 0,
        "completed": ["command"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert "command_not_verified" in evaluate(world, report).violations


def test_terminal_scenario_requires_session_write_and_exit_evidence():
    """后台终端必须保留同一会话句柄，并在 stdin 写入后读到稳定回复和零退出码。"""
    world = EvaluationWorld("terminal_session")
    session_id = "term_test"
    world.record_command(world.scenario.command, {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "\n[标准输出]\nREADY\n",
    }, action="start")
    world.record_command("", {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "",
    }, action="write", session_id=session_id, input_text="MOVIEPILOT_TERMINAL_OK\n")
    world.record_command("", {
        "execution_outcome": "succeeded", "status": "exited", "session_id": session_id,
        "exit_code": 0, "output": "\n[标准输出]\nREPLY=MOVIEPILOT_TERMINAL_OK\n",
    }, action="wait", session_id=session_id)
    report = {
        "status": "completed", "terminal_output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n", "terminal_exit_code": 0,
        "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert evaluate(world, report).passed is True


def test_terminal_native_aggregate_without_interaction_cannot_prove_stdin():
    """原生 CLI 只有聚合输出时不能把输入标记当作真实 stdin 事件。"""
    world = EvaluationWorld("terminal_session")
    command = "/bin/zsh -lc " + shlex.quote(world.scenario.command)
    world.record_command(command, {
        "execution_outcome": "succeeded", "status": "exited", "exit_code": 0,
        "output": "READY\r\nMOVIEPILOT_TERMINAL_OK\r\nREPLY=MOVIEPILOT_TERMINAL_OK\r\n",
        "terminal_input_observed": False,
    }, action="start")
    report = {
        "status": "completed", "terminal_output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n", "terminal_exit_code": 0,
        "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert "terminal_input_not_verified" in evaluate(world, report).violations


def test_terminal_pty_echo_is_part_of_the_verified_output():
    """PTY 回显写入内容时，验收器保留该真实输出并继续核验回复与退出码。"""
    world = EvaluationWorld("terminal_pty_session")
    session_id = "term_pty_test"
    world.record_command(world.scenario.command, {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "READY\r\n",
    }, action="start")
    world.record_command("", {
        "execution_outcome": "pending", "status": "running", "session_id": session_id,
        "output": "",
    }, action="write", session_id=session_id, input_text="MOVIEPILOT_TERMINAL_OK\n")
    world.record_command("", {
        "execution_outcome": "succeeded", "status": "exited", "session_id": session_id,
        "exit_code": 0, "output": "MOVIEPILOT_TERMINAL_OK\r\nREPLY=MOVIEPILOT_TERMINAL_OK\r\n",
    }, action="wait", session_id=session_id)
    report = {
        "status": "completed",
        "terminal_output": "READY\r\nMOVIEPILOT_TERMINAL_OK\r\nREPLY=MOVIEPILOT_TERMINAL_OK\r\n",
        "terminal_exit_code": 0, "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert evaluate(world, report).passed is True


def test_terminal_one_shot_command_cannot_claim_interactive_completion():
    """一次性 run 即使输出相同标记，也不能冒充启动、写入和等待过终端会话。"""
    world = EvaluationWorld("terminal_session")
    world.record_command(world.scenario.command, {
        "execution_outcome": "succeeded", "status": "exited", "exit_code": 0,
        "output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n",
    }, action="run")
    report = {
        "status": "completed", "terminal_output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n", "terminal_exit_code": 0,
        "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    violations = set(evaluate(world, report).violations)
    assert {"terminal_start_not_verified", "terminal_input_not_verified", "terminal_output_not_read"} <= violations


def test_terminal_wrong_stdin_is_not_recovered_by_correct_final_text():
    """错误输入不能靠正确的最终文字抵消交互合同失败。"""
    world = EvaluationWorld("terminal_session")
    session_id = "term_test"
    world.record_command(world.scenario.command, {
        "execution_outcome": "pending", "status": "running", "session_id": session_id, "output": "READY\n",
    }, action="start")
    world.record_command("", {
        "execution_outcome": "failed", "status": "error", "session_id": session_id, "output": "",
    }, action="write", session_id=session_id, input_text="wrong\n")
    report = {
        "status": "completed", "terminal_output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n", "terminal_exit_code": 0,
        "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert "terminal_input_not_verified" in evaluate(world, report).violations


def test_browser_scenario_requires_dynamic_page_observation():
    """浏览器场景没有页面回执时，即使报告文本正确也不能通过。"""
    world = EvaluationWorld("browser_navigation")
    report = {
        "status": "completed", "browser_text": "BROWSER_OK", "completed": ["browser"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert "browser_not_verified" in evaluate(world, report).violations
    world.record_browser("get_content", {"success": True, "execution_outcome": "succeeded", "content": "BROWSER_OK"})
    assert evaluate(world, report).passed is True


def test_cli_reports_failure_without_executing_any_model(tmp_path):
    """独立命令行真实退出码与报告表达失败，且显式标注只是离线轨迹验收。"""
    trace = tmp_path / "trace.json"
    report = tmp_path / "report.json"
    trace.write_text(json.dumps({"calls": [], "final": {"status": "completed"}}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.evaluation", "--scenario", "dedup_existing", "--replay", str(trace), "--output", str(report)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["passed"] is False
    assert payload["intelligence_evaluated"] is False
    assert json.loads(report.read_text()) == payload
