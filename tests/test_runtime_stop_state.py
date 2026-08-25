"""进程停止契约和 global_vars 兼容边界测试。"""

import threading
from pathlib import Path

from app.runtime.config import GlobalVar
from app.runtime.stop import ProcessStopState, runtime_stop_state


def test_process_stop_state_separates_repeatable_workflow_and_one_shot_transfer() -> None:
    """工作流停止可恢复，整理路径停止只消费一次。"""
    state = ProcessStopState()

    state.stop_workflow(7)
    assert state.is_workflow_stopped(7)
    state.resume_workflow(7)
    assert not state.is_workflow_stopped(7)

    state.stop_transfer("/media/a.mkv")
    assert state.consume_transfer_stop("/media/a.mkv")
    assert not state.consume_transfer_stop("/media/a.mkv")


def test_system_stop_propagates_to_all_cancellation_scopes() -> None:
    """系统停止后所有工作流和整理任务都必须立即观察到停止。"""
    state = ProcessStopState()

    state.stop_system()

    assert state.is_system_stopped
    assert state.is_workflow_stopped(99)
    assert state.consume_transfer_stop("/unknown")


def test_global_vars_stop_api_delegates_to_runtime_contract(monkeypatch) -> None:
    """旧 global_vars ABI 必须与新的显式停止状态共享同一事实源。"""
    event = threading.Event()
    monkeypatch.setattr(runtime_stop_state, "_system_event", event)
    legacy = GlobalVar()

    legacy.stop_system()

    assert event.is_set()
    assert legacy.is_system_stopped


def test_host_code_no_longer_reads_stop_state_from_global_vars() -> None:
    """除兼容实现外，宿主不得重新从 global_vars 读取任何停止信号。"""
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "global_vars.is_system_stopped",
        "global_vars.stop_system",
        "global_vars.is_workflow_stopped",
        "global_vars.stop_workflow",
        "global_vars.workflow_resume",
        "global_vars.is_transfer_stopped",
        "global_vars.stop_transfer",
    )
    violations = []
    for path in (root / "app").rglob("*.py"):
        if path == root / "app/runtime/config.py" or "app/plugins" in path.as_posix():
            continue
        content = path.read_text(encoding="utf-8")
        for expression in forbidden:
            if expression in content:
                violations.append(f"{path.relative_to(root)}:{expression}")

    assert violations == []
