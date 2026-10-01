"""本地 CLI 更新遇到源码改动时的确认与清理测试。"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """加载独立的本地安装脚本模块，避免共享全局配置。"""
    module_name = f"moviepilot_local_setup_update_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dirty_update_clears_tracked_changes_after_confirmation(monkeypatch):
    """确认清理后应重置已跟踪源码改动，供后续 Git 更新继续。"""
    module = load_local_setup_module()
    prompts: list[tuple[str, bool]] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_git_output", lambda *_args: " M app/example.py\n")
    monkeypatch.setattr(
        module,
        "_prompt_yes_no",
        lambda label, default: prompts.append((label, default)) or True,
    )
    monkeypatch.setattr(
        module,
        "run",
        lambda command, cwd=None: commands.append(command),
    )

    module._ensure_git_clean()

    assert prompts == [
        (
            "检测到当前仓库有未提交的源码改动：app/example.py，是否清空本地改动并继续更新",
            False,
        )
    ]
    assert commands == [["git", "reset", "--hard", "HEAD"]]


def test_dirty_update_stops_when_local_change_cleanup_is_rejected(monkeypatch):
    """拒绝清理时应取消更新且不执行任何 Git 写操作。"""
    module = load_local_setup_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_git_output", lambda *_args: " M app/example.py\n")
    monkeypatch.setattr(module, "_prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        module,
        "run",
        lambda command, cwd=None: commands.append(command),
    )

    with pytest.raises(RuntimeError, match="已取消更新"):
        module._ensure_git_clean()

    assert commands == []


def test_dirty_update_stops_without_an_interactive_input(monkeypatch):
    """没有可用输入时应取消更新，而不是默认清除源码改动。"""
    module = load_local_setup_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_git_output", lambda *_args: " M app/example.py\n")

    def raise_eof(*_args, **_kwargs):
        """模拟没有可用终端输入。"""
        raise EOFError

    monkeypatch.setattr(module, "_prompt_yes_no", raise_eof)
    monkeypatch.setattr(
        module,
        "run",
        lambda command, cwd=None: commands.append(command),
    )

    with pytest.raises(RuntimeError, match="不支持交互确认"):
        module._ensure_git_clean()

    assert commands == []
