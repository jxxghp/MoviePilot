"""
工具循环检测的阈值、恢复及回执语义回归。
"""

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
    """
    构造无人值守的默认强制停止策略。
    """
    return ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"non_interactive_hard_stop_enabled": False},
        platform="telegram",
    )

    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is False


def test_default_repeated_identical_failed_call_warns_without_blocking():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_identical_call_streak_halts_any_tool_when_hard_stop_enabled():
    # 对应 #89069 / #100849：重复成功的 terminal/skill_view 调用可能绕过幂等工具的无进展判定。
    # observe_call 对所有工具统计连续相同的调用与结果，启用硬停止时必须阻断。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    soft = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, no_progress_block_after=2)
    )
    for _ in range(6):
        soft.observe_call("terminal", {"command": "ls"}, "a\nb\n", failed=False)
    # 交互会话中只发出提示。
    assert soft.halt_decision is None

    hard = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    )
    for _ in range(6):
        hard.observe_call("process_manage", {"action": "poll", "session_id": "p1"}, "running", failed=False)
    # 状态未变化的轮询仍是合法操作。
    assert hard.halt_decision is None

    # 结果变化后重新计算连续重复次数。
    for i in range(6):
        hard.observe_call("terminal", {"command": "date"}, f"t{i}", failed=False)
    assert hard.halt_decision is None


def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 表示不限制；负数或无效内容回退到默认限制。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == LoopCapConfig().max_web_searches
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == LoopCapConfig().max_subagents


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # 即使关闭单轮循环检测的硬停止开关，循环次数上限仍应生效。
    # 每次使用不同查询避开重复调用检测，确保阻断来自总次数限制。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    assert c.before_call("terminal", args).allows_execution
    return c.after_call("terminal", args, _RED, failed=True)


def test_fix_retest_loop_is_never_hard_stopped():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    c = _HARD()
    for i in range(12):
        d = _run_red(c)
        assert not d.should_halt, f"halted on red run {i + 1}"
        # 两次执行之间发生实际修改，应视为取得进展。
        c.after_call("patch", {"path": "x.py", "old_string": "a", "new_string": f"b{i}"},
                     '{"success": true, "diff": "..."}', failed=False)
    assert c.halt_decision is None
    assert c.before_call("terminal", _PYTEST).allows_execution


def test_pure_replay_with_no_intervening_change_is_still_blocked():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    c = _HARD()
    for _ in range(5):
        _run_red(c)
    d = c.before_call("terminal", _PYTEST)
    assert d.action == "block" and d.code == "repeated_exact_failure_block"


def test_intervening_mutation_resets_the_replay_streak_only_once():
    # 四次失败后修改一次，再连续失败四次；修改会重置计数。
    # 后续第五次没有修改的重试才应被阻断。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    c = _HARD()
    for _ in range(4):
        _run_red(c)
    c.after_call("write_file", {"path": "x.py", "content": "y"}, '{"bytes_written": 1}', failed=False)
    for _ in range(5):
        assert c.before_call("terminal", _PYTEST).allows_execution
        c.after_call("terminal", _PYTEST, _RED, failed=True)
    assert c.before_call("terminal", _PYTEST).action == "block"


def test_distinct_failing_terminal_commands_warn_but_never_halt():
    # 模拟诊断检查中的无匹配搜索、缺失命令和构建失败。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    c = _HARD()
    for i in range(12):
        args = {"command": f"grep -q needle{i} haystack.txt"}
        d = c.after_call("terminal", args, _RED, failed=True)
        assert not d.should_halt, f"same_tool halt on distinct command #{i + 1}"
    assert c.halt_decision is None
    # 不允许容忍失败的工具，即使八次失败各不相同也会停止。
    c2 = _HARD()
    last = None
    for i in range(8):
        last = c2.after_call("send_message", {"to": f"u{i}"}, '{"error": "no route"}', failed=True)
    assert last.should_halt and last.code == "same_tool_failure_halt"


def test_browser_retry_after_action_is_not_a_replay():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    c = _HARD()
    nav = {"url": "https://example.test/app"}
    for _ in range(8):
        assert c.before_call("browser_navigate", nav).allows_execution
        c.after_call("browser_navigate", nav, '{"error": "timeout"}', failed=True)
        c.after_call("browser_click", {"selector": "#retry"}, '{"ok": true}', failed=False)
    assert c.halt_decision is None


def test_supervised_task_platforms_keep_warning_only_default():
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    for platform in ("subagent", "api_server", "cli"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is False, platform
    for platform in ("telegram", "discord", "cron", "kanban"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is True, platform


def test_a_real_tool_error_is_still_a_failure():
    """
    豁免以专用标记为依据，不凭文本内容判断。
    真实失败仍需计入连续次数，避免失去阻止重复失败循环的能力。
    """

    real = '{"error": "ENOENT: no such file"}'
    assert classify_tool_failure("read_file", real)[0] is True
    # 豁免标记仅接受布尔值 True，不能把非空文本视为有效标记。
    assert classify_tool_failure("read_file", '{"error": "x", "guardrail_refusal": "yes"}')[0] is True


def test_identical_streak_ignores_volatile_execution_metadata():
    # execute_code 每次执行都会改变 execution_count、duration_seconds 等元数据。
    # 若将这些值纳入哈希，重复空调用会被误判为新结果，曾导致同一探测执行 147 次而未停止。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"code": "import subprocess\nprint(subprocess.run(['true']).returncode) if False else None"}

    def result(n):
        """
        验证循环检测合同，保持阈值、恢复及回执语义。
        """
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
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    # 仅 execute_code 的已知元数据位置可忽略；其他工具的同名字段可能就是业务结果。
    # 例如任务状态中的耗时发生变化，应视为新结果而非连续重复。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
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
    # 对应 #124072：交互界面关闭硬停止后，只能通过追加提示提醒模型。
    # 过去 186 次空操作仅改变执行计数和耗时，却未触发任何重复提示。
    """
    验证循环检测合同，保持阈值、恢复及回执语义。
    """
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({}, platform="desktop"))
    args = {"code": 'print("...")'}

    def result(n):
        """
        验证循环检测合同，保持阈值、恢复及回执语义。
        """
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
