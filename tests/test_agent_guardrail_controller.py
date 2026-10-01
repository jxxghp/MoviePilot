"""工具循环检测的阈值、恢复及回执语义回归。"""

import json

from app.agent.guardrails.controller import (
    LoopCapConfig,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)

_PYTEST = {'command': 'pytest tests/test_x.py -q'}
_RED = '{"output": "1 failed", "exit_code": 1}'


def _HARD():
    """构造无人值守的默认强制停止策略。"""
    return ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_gateway_platform_defaults_to_hard_stop_without_changing_interactive_defaults():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    interactive_configs = [
        ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        for platform in ("cli", "tui", "desktop", "acp")
    ]
    telegram_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="telegram")
    cron_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="cron")

    assert all(cfg.hard_stop_enabled is False for cfg in interactive_configs)
    assert telegram_cfg.hard_stop_enabled is True
    assert cron_cfg.hard_stop_enabled is True


def test_non_interactive_hard_stop_can_be_disabled_explicitly():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"non_interactive_hard_stop_enabled": False},
        platform="telegram",
    )

    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is False


def test_default_repeated_identical_failed_call_warns_without_blocking():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_skill_read_tools_are_idempotent_and_block_repeated_identical_success_output():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    cases = [
        (
            "skill_view",
            {"name": "gui-agent-ml-operations"},
            '{"success":true,"name":"gui-agent-ml-operations","content":"same"}',
        ),
        (
            "skills_list",
            {"category": "mlops"},
            '{"success":true,"skills":[{"name":"gui-agent-ml-operations"}]}',
        ),
    ]

    for tool_name, args, result in cases:
        controller = ToolCallGuardrailController(
            ToolCallGuardrailConfig(
                hard_stop_enabled=True,
                no_progress_warn_after=2,
                no_progress_block_after=2,
            )
        )

        assert controller.before_call(tool_name, args).action == "allow"
        assert controller.after_call(tool_name, args, result, failed=False).action == "allow"
        assert controller.before_call(tool_name, args).action == "allow"
        warn = controller.after_call(tool_name, args, result, failed=False)
        assert warn.action == "warn"
        assert warn.code == "idempotent_no_progress_warning"

        blocked = controller.before_call(tool_name, args)
        assert blocked.action == "block"
        assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_identical_call_streak_halts_any_tool_when_hard_stop_enabled():
    # #89069 / #100849 bundle: a model replaying the same SUCCESSFUL
    # terminal/skill_view call with a byte-identical result is not covered by
    # the idempotent_tools no-progress block. The consecutive-identical
    # streak (observe_call) is tool-agnostic; under hard_stop it must halt.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"command": "hermes config get memory.provider"}
    for i in range(1, 5):
        controller.after_call("terminal", args, "local\n", failed=False)
        controller.observe_call("terminal", args, "local\n", failed=False)
        assert controller.halt_decision is None, f"halted early at {i}"

    controller.after_call("terminal", args, "local\n", failed=False)
    controller.observe_call("terminal", args, "local\n", failed=False)
    halt = controller.halt_decision
    assert halt is not None and halt.should_halt
    assert halt.code == "identical_call_streak_halt"
    assert halt.tool_name == "terminal" and halt.count == 5


def test_identical_call_streak_never_halts_when_hard_stop_disabled_or_for_pollers():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    soft = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, no_progress_block_after=2)
    )
    for _ in range(6):
        soft.observe_call("terminal", {"command": "ls"}, "a\nb\n", failed=False)
    assert soft.halt_decision is None  # notice-only in interactive sessions

    hard = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    )
    for _ in range(6):
        hard.observe_call("process_manage", {"action": "poll", "session_id": "p1"}, "running", failed=False)
    assert hard.halt_decision is None  # an unchanged poll is legitimate progress

    # A changed result resets the streak.
    for i in range(6):
        hard.observe_call("terminal", {"command": "date"}, f"t{i}", failed=False)
    assert hard.halt_decision is None


def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == LoopCapConfig().max_web_searches
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == LoopCapConfig().max_subagents


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True


def _run_red(c, args=_PYTEST):
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    assert c.before_call("terminal", args).allows_execution
    return c.after_call("terminal", args, _RED, failed=True)


def test_fix_retest_loop_is_never_hard_stopped():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    c = _HARD()
    for i in range(12):
        d = _run_red(c)
        assert not d.should_halt, f"halted on red run {i + 1}"
        # the model edits between runs — a landed mutation is progress
        c.after_call("patch", {"path": "x.py", "old_string": "a", "new_string": f"b{i}"},
                     '{"success": true, "diff": "..."}', failed=False)
    assert c.halt_decision is None
    assert c.before_call("terminal", _PYTEST).allows_execution


def test_pure_replay_with_no_intervening_change_is_still_blocked():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    c = _HARD()
    for _ in range(5):
        _run_red(c)
    d = c.before_call("terminal", _PYTEST)
    assert d.action == "block" and d.code == "repeated_exact_failure_block"


def test_intervening_mutation_resets_the_replay_streak_only_once():
    # 4 reds, one edit, then 4 reds with NO edit: the second run of 4 is a
    # fresh streak, and the 5th unchanged retry after it is blocked.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    c = _HARD()
    for _ in range(4):
        _run_red(c)
    c.after_call("write_file", {"path": "x.py", "content": "y"}, '{"bytes_written": 1}', failed=False)
    for _ in range(5):
        assert c.before_call("terminal", _PYTEST).allows_execution
        c.after_call("terminal", _PYTEST, _RED, failed=True)
    assert c.before_call("terminal", _PYTEST).action == "block"


def test_distinct_failing_terminal_commands_warn_but_never_halt():
    # A diagnostic sweep: grep with no matches, missing binaries, red builds.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    c = _HARD()
    for i in range(12):
        args = {"command": f"grep -q needle{i} haystack.txt"}
        d = c.after_call("terminal", args, _RED, failed=True)
        assert not d.should_halt, f"same_tool halt on distinct command #{i + 1}"
    assert c.halt_decision is None
    # ...while a non-tolerant tool failing 8 distinct ways still halts.
    c2 = _HARD()
    last = None
    for i in range(8):
        last = c2.after_call("send_message", {"to": f"u{i}"}, '{"error": "no route"}', failed=True)
    assert last.should_halt and last.code == "same_tool_failure_halt"


def test_browser_retry_after_action_is_not_a_replay():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    c = _HARD()
    nav = {"url": "https://example.test/app"}
    for _ in range(8):
        assert c.before_call("browser_navigate", nav).allows_execution
        c.after_call("browser_navigate", nav, '{"error": "timeout"}', failed=True)
        c.after_call("browser_click", {"selector": "#retry"}, '{"ok": true}', failed=False)
    assert c.halt_decision is None


def test_supervised_task_platforms_keep_warning_only_default():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    for platform in ("subagent", "api_server", "cli"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is False, platform
    for platform in ("telegram", "discord", "cron", "kanban"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is True, platform


def test_a_real_tool_error_is_still_a_failure():
    """The exemption is keyed on the marker, not on the word: a body that
    genuinely failed still counts, or the streak that stops a real loop is gone."""

    real = '{"error": "ENOENT: no such file"}'
    assert classify_tool_failure("read_file", real)[0] is True
    # The marker is only honoured as the literal boolean, never as truthy prose.
    assert classify_tool_failure("read_file", '{"error": "x", "guardrail_refusal": "yes"}')[0] is True


def test_identical_streak_ignores_volatile_execution_metadata():
    # execute_code results carry per-call metadata (execution_count, duration_seconds) that
    # changes on every run. Hashing it made each empty replay look "new", so a model re-ran
    # the same empty probe 147 times without the identical-call halt ever firing.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"code": "import subprocess\nprint(subprocess.run(['true']).returncode) if False else None"}

    def result(n):
        """验证循环检测合同，保持阈值、恢复及回执语义。"""
        return json.dumps({
            "status": "success", "output": "", "exit_code": 0, "tool_calls_made": 0,
            "duration_seconds": 1.0 + n / 100,
            "kernel": {"mode": "session", "reused": True, "execution_count": 100 + n, "state_reset": False},
            "stdout_truncated": False, "stdout_bytes_captured": 0,
        })

    for i in range(4):
        controller.observe_call("execute_code", args, result(i), failed=False)
        assert controller.halt_decision is None, f"halted early at {i}"
    controller.observe_call("execute_code", args, result(4), failed=False)
    halt = controller.halt_decision
    assert halt is not None, "volatile metadata defeated the identical-call streak"
    assert halt.code == "identical_call_streak_halt"


def test_identical_streak_still_resets_when_real_output_changes():
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=3)
    )
    args = {"code": "print(1)"}
    for i in range(6):
        out = json.dumps({"status": "success", "output": f"line {i}", "duration_seconds": 1.0,
                          "kernel": {"execution_count": i}})
        controller.observe_call("execute_code", args, out, failed=False)
    assert controller.halt_decision is None, "different real output must not count as a replay"


def test_other_tools_keep_duration_and_execution_count_as_real_output():
    # Only execute_code's known metadata locations are volatile. For any other tool these keys
    # can be the actual answer (e.g. a job-status tool reporting how long a job ran), so results
    # that differ only there are different results and must not form an identical streak.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=3)
    )
    args = {"job": "build-42"}
    for i in range(6):
        out = json.dumps({"status": "done", "duration_seconds": 10 + i,
                          "stats": {"execution_count": 100 + i}})
        controller.observe_call("mcp_ci_job_status", args, out, failed=False)
    assert controller.halt_decision is None, "a real change in duration_seconds/execution_count was treated as a replay"


def test_execute_code_replay_streak_notice_fires_on_warn_only_desktop_config():
    # #124072: on an interactive surface hard stops are off, so the appended notice is
    # the only signal the model gets. 186 no-op print("...") cells whose results differed
    # only in kernel.execution_count / duration_seconds produced zero notices.
    """验证循环检测合同，保持阈值、恢复及回执语义。"""
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({}, platform="desktop"))
    args = {"code": 'print("...")'}

    def result(n):
        """验证循环检测合同，保持阈值、恢复及回执语义。"""
        return json.dumps({
            "status": "success", "output": "...\n", "exit_code": 0, "tool_calls_made": 0,
            "duration_seconds": 0.001 * n,
            "kernel": {"mode": "session", "reused": True, "execution_count": n, "state_reset": False},
            "stdout_truncated": False, "stdout_bytes_captured": 4, "stdout_bytes_total": 4,
            "stdout_bytes_omitted": 0,
        })

    notices = [
        controller.observe_call("execute_code", args, result(i), tool_call_id=f"c{i}").notice
        for i in range(1, 7)
    ]
    assert notices[:2] == [None, None]
    assert all(n is not None and "consecutive identical call to execute_code" in n for n in notices[2:]), notices
    assert controller.halt_decision is None, "warn-only surfaces must not halt"
