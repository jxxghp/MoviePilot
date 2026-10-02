"""本地 CLI 更新遇到源码改动时的确认与清理测试。"""

from __future__ import annotations

import importlib.util
import subprocess
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


@pytest.fixture
def moved_tag_repository(tmp_path):
    """用临时本地仓库模拟远端重建同名标签，不访问外部网络。"""
    remote = tmp_path / "remote"
    checkout = tmp_path / "checkout"
    remote.mkdir()

    def git(path, *args):
        """隔离 Git 身份和签名配置，执行测试仓库操作。"""
        return subprocess.check_output(
            ["git", "-c", "user.name=CLI Test", "-c", "user.email=cli@example.test",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", str(path), *args],
            text=True,
        ).strip()

    git(remote, "init", "-b", "v3")
    git(remote, "commit", "--allow-empty", "-m", "initial")
    git(remote, "tag", "v3.1.0")
    old_sha = git(remote, "rev-parse", "HEAD")
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "tag", "local-only")
    git(remote, "commit", "--allow-empty", "-m", "rebuild")
    git(remote, "tag", "--force", "v3.1.0")
    return checkout, git, old_sha, git(remote, "rev-parse", "HEAD")


@pytest.mark.parametrize("ref", ["latest", "v3", "v3.1.0"])
def test_update_overwrites_moved_remote_tag(monkeypatch, moved_tag_repository, ref):
    """同名标签随远端更新，同时保留远端没有的本地标签。"""
    checkout, git, old_sha, new_sha = moved_tag_repository
    module = load_local_setup_module()
    monkeypatch.setattr(module, "ROOT", checkout)

    assert module._update_backend_ref(ref) == ("v3" if ref == "latest" else ref)

    assert git(checkout, "rev-parse", "v3.1.0") == new_sha
    assert git(checkout, "rev-parse", "HEAD") == new_sha
    assert git(checkout, "rev-parse", "local-only") == old_sha


def test_update_moved_tag_does_not_reset_diverged_branch(monkeypatch, moved_tag_repository):
    """强制同步标签不得覆盖分叉分支上的本地提交。"""
    checkout, git, _, new_sha = moved_tag_repository
    git(checkout, "commit", "--allow-empty", "-m", "local work")
    local_sha = git(checkout, "rev-parse", "HEAD")
    module = load_local_setup_module()
    monkeypatch.setattr(module, "ROOT", checkout)

    with pytest.raises(subprocess.CalledProcessError):
        module._update_backend_ref("latest")

    assert git(checkout, "rev-parse", "HEAD") == local_sha
    assert git(checkout, "rev-parse", "v3.1.0") == new_sha


def test_offline_update_preserves_local_tag(monkeypatch, moved_tag_repository):
    """离线更新只使用已验证的本地标签，即使远端已更换标签。"""
    checkout, git, old_sha, _ = moved_tag_repository
    git(checkout, "remote", "remove", "origin")
    module = load_local_setup_module()
    monkeypatch.setattr(module, "ROOT", checkout)

    assert module._update_backend_ref("v3.1.0", fetch=False) == "v3.1.0"

    assert git(checkout, "rev-parse", "HEAD") == old_sha
