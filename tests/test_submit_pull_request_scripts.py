"""submit-pull-request Skill 的离线单元测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "submit-pull-request" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import clone_repository as clone_script  # noqa: E402
import collect_pull_request as collect_script  # noqa: E402
import pr_common as common  # noqa: E402
import prepare_pull_request as prepare_script  # noqa: E402
import submit_pull_request as submit_script  # noqa: E402


def _git(root: Path, *arguments: str) -> str:
    """运行测试所需的本地 Git 命令。"""
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _repo(tmp_path: Path) -> Path:
    """创建有初始 Commit 的临时 clone 替身。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", str(root))
    _git(root, "config", "user.name", "MoviePilot Test")
    _git(root, "config", "user.email", "moviepilot-test@example.invalid")
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (root / "deleted.py").write_text("remove = True\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    _git(root, "branch", "-M", "main")
    return root


def _set_runtime_dir(monkeypatch, path: Path) -> None:
    """把 Skill 临时文件指向当前测试目录。"""
    monkeypatch.setattr(common, "get_runtime_setting", lambda key: str(path))


def test_collect_requires_a_real_git_clone(tmp_path: Path) -> None:
    """没有 Git 仓库时必须明确失败，不回退到文件清单模式。"""
    with pytest.raises(common.PullRequestError) as error:
        common.collect_changes(tmp_path)
    assert error.value.reason == "git_required"


def test_collect_records_tracked_untracked_and_deleted_files(tmp_path: Path) -> None:
    """采集器应覆盖修改、新增和删除，并保留模式与摘要。"""
    root = _repo(tmp_path)
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (root / "new.py").write_text("created = True\n", encoding="utf-8")
    (root / "deleted.py").unlink()

    mode, changes = common.collect_changes(root)

    assert mode == "git"
    assert {item["path"] for item in changes} == {"tracked.py", "new.py", "deleted.py"}
    operations = {item["path"]: item["operation"] for item in changes}
    assert operations == {"deleted.py": "delete", "new.py": "create", "tracked.py": "update"}
    assert all(item["mode"] == "100644" for item in changes)
    assert all(item["after_sha256"] is not None for item in changes if item["operation"] != "delete")


def test_collect_rejects_runtime_paths(tmp_path: Path) -> None:
    """运行时和敏感路径不能进入 PR。"""
    root = _repo(tmp_path)
    (root / ".env").write_text("SECRET=not-for-pr\n", encoding="utf-8")
    with pytest.raises(common.PullRequestError) as error:
        common.collect_changes(root)
    assert error.value.reason == "sensitive_path"


def test_changes_preview_detects_post_preview_edit(tmp_path: Path, monkeypatch) -> None:
    """用户确认后若文件再次变化，提交前必须拒绝。"""
    root = _repo(tmp_path)
    _set_runtime_dir(monkeypatch, tmp_path / "runtime")
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    collected = collect_script.collect_pull_request(str(root))
    changes_file = Path(collected["changes_file"])
    (root / "tracked.py").write_text("value = 3\n", encoding="utf-8")

    with pytest.raises(common.PullRequestError) as error:
        common.changes_from_payload(
            {
                "source_root": str(root),
                "changes_file": str(changes_file),
            }
        )
    assert error.value.reason == "changes_modified"


def test_ensure_fork_reuses_existing_fork() -> None:
    """已有且 parent 正确的 Fork 不应重复创建。"""

    class FakeClient:
        """已有 Fork 场景的最小 GitHub API 替身。"""

        def __init__(self) -> None:
            """记录是否错误地请求了创建 Fork。"""
            self.created = False

        def get_repo(self, repo: str) -> dict:
            """返回目标 Fork 及其 parent。"""
            assert repo == "bot/MoviePilot"
            return {
                "fork": True,
                "full_name": repo,
                "parent": {"full_name": "jxxghp/MoviePilot"},
            }

        def create_fork(self, repo: str) -> dict:
            """记录创建调用，测试应保持为零次。"""
            self.created = True
            return {}

    client = FakeClient()
    fork_repo, created = common.ensure_fork(client, "jxxghp/MoviePilot", "bot")
    assert fork_repo == "bot/MoviePilot"
    assert created is False
    assert client.created is False


def test_ensure_fork_creates_after_not_found(monkeypatch) -> None:
    """不存在的 Fork 只创建一次，并等待异步创建完成。"""

    class FakeClient:
        """异步 Fork 创建场景的最小 GitHub API 替身。"""

        def __init__(self) -> None:
            """初始化查询和创建计数。"""
            self.lookups = 0
            self.created = 0

        def get_repo(self, repo: str) -> dict:
            """第一次查询返回 404，随后返回已创建 Fork。"""
            self.lookups += 1
            if self.lookups == 1:
                raise common.GitHubApiError("github_api_error", "not found", status=404)
            return {"fork": True, "parent": {"full_name": "jxxghp/MoviePilot"}}

        def create_fork(self, repo: str) -> dict:
            """记录一次 Fork 创建请求。"""
            self.created += 1
            return {}

    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    client = FakeClient()
    fork_repo, created = common.ensure_fork(client, "jxxghp/MoviePilot", "bot")
    assert fork_repo == "bot/MoviePilot"
    assert created is True
    assert client.created == 1


def test_clone_repository_uses_upstream_base_and_writes_state(tmp_path: Path, monkeypatch) -> None:
    """clone 脚本应从上游基线创建新分支，而不是直接修改 Fork 默认分支。"""
    source = _repo(tmp_path)
    upstream_bare = tmp_path / "upstream.git"
    fork_bare = tmp_path / "fork.git"
    _git(tmp_path, "clone", "--bare", str(source), str(upstream_bare))
    _git(tmp_path, "clone", "--bare", str(source), str(fork_bare))
    base_sha = _git(source, "rev-parse", "main")

    class FakeClient:
        """clone 脚本 API 依赖的最小替身。"""

        def get_user(self) -> dict:
            """返回当前用户。"""
            return {"login": "bot"}

        def get_repo(self, repo: str) -> dict:
            """返回目标仓库默认分支。"""
            return {"default_branch": "main"}

        def get_ref(self, repo: str, branch: str) -> dict:
            """返回上游基线，工作分支模拟不存在。"""
            if repo == "jxxghp/MoviePilot":
                return {"object": {"sha": base_sha}}
            raise common.GitHubApiError("github_api_error", "not found", status=404)

    monkeypatch.setattr(clone_script, "GitHubClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(
        clone_script,
        "load_github_headers",
        lambda repo: ({"Authorization": "Bearer test-token"}, None),
    )
    monkeypatch.setattr(
        clone_script,
        "ensure_fork",
        lambda client, target, login: ("bot/MoviePilot", False),
    )
    monkeypatch.setattr(
        clone_script,
        "github_clone_url",
        lambda repo: str(fork_bare if repo == "bot/MoviePilot" else upstream_bare),
    )

    @contextmanager
    def fake_git_auth(headers):
        """不让离线测试创建真实认证脚本。"""
        yield os.environ.copy()

    monkeypatch.setattr(clone_script, "git_auth_environment", fake_git_auth)
    result = clone_script.clone_repository(
        "jxxghp/MoviePilot",
        destination=str(tmp_path / "clone"),
        state_file=str(tmp_path / "state.json"),
    )

    clone_root = Path(result["source_root"])
    assert result["branch"].startswith("agent/moviepilot/")
    assert _git(clone_root, "rev-parse", "HEAD") == base_sha
    assert _git(clone_root, "remote", "get-url", "origin") == str(fork_bare)
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["fork_repo"] == "bot/MoviePilot"


def test_prepare_without_token_stops_before_api_write(tmp_path: Path, monkeypatch) -> None:
    """没有 Token 时预览也应明确阻塞，而不是伪造可提交状态。"""
    root = _repo(tmp_path)
    _set_runtime_dir(monkeypatch, tmp_path / "runtime")
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    collected = collect_script.collect_pull_request(str(root))
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(
        json.dumps(
            {
                "target_repo": "jxxghp/MoviePilot",
                "source_root": str(root),
                "changes_file": collected["changes_file"],
                "title": "[修复] 测试",
                "commit_message": "fix: test",
                "body": "## 验证\n- pytest",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(prepare_script, "load_github_headers", lambda repo: ({}, None))

    with pytest.raises(common.PullRequestError) as error:
        prepare_script.prepare_pull_request(str(draft_file))
    assert error.value.reason == "no_token"


def test_submit_commits_pushes_and_creates_pr_with_fake_github(tmp_path: Path, monkeypatch) -> None:
    """提交脚本应真实创建本地 Commit，但用假 API 隔离外网副作用。"""
    root = _repo(tmp_path)
    _git(root, "remote", "add", "origin", "https://github.com/bot/MoviePilot.git")
    base_sha = _git(root, "rev-parse", "HEAD")
    branch = "agent/moviepilot/test-branch"
    _git(root, "switch", "-c", branch)
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    _set_runtime_dir(monkeypatch, tmp_path / "runtime")
    collected = collect_script.collect_pull_request(str(root))
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps(
            {
                "target_repo": "jxxghp/MoviePilot",
                "fork_repo": "bot/MoviePilot",
                "source_root": str(root),
                "changes_file": collected["changes_file"],
                "base": "main",
                "expected_base_sha": base_sha,
                "initial_head_sha": base_sha,
                "branch": branch,
                "github_login": "bot",
                "title": "[修复] 测试提交",
                "commit_message": "fix: test submit",
                "body": "## 验证\n- pytest",
                "draft": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeClient:
        """提交流程使用的最小 GitHub API 替身。"""

        def __init__(self, **kwargs) -> None:
            """初始化远端分支状态。"""
            self.remote_sha = None

        def get_user(self) -> dict:
            """返回预览时使用的 Token 用户。"""
            return {"login": "bot"}

        def get_repo(self, repo: str) -> dict:
            """返回目标 Fork parent。"""
            return {"fork": True, "parent": {"full_name": "jxxghp/MoviePilot"}}

        def get_ref(self, repo: str, ref_branch: str) -> dict:
            """返回上游基线或模拟推送后的 Fork 分支。"""
            if repo == "jxxghp/MoviePilot":
                return {"object": {"sha": base_sha}}
            if self.remote_sha:
                return {"object": {"sha": self.remote_sha}}
            raise common.GitHubApiError("github_api_error", "not found", status=404)

        def list_open_pulls(self, repo: str, *, head: str, base: str) -> list[dict]:
            """模拟尚无开放 PR。"""
            return []

        def create_pull(self, repo: str, payload: dict) -> dict:
            """返回已验证的 PR 对象。"""
            return {
                "html_url": "https://github.com/jxxghp/MoviePilot/pull/1",
                "number": 1,
                "draft": payload["draft"],
                "head": {"ref": branch, "sha": self.remote_sha},
                "base": {"ref": payload["base"]},
            }

    fake_client = FakeClient()
    monkeypatch.setattr(submit_script, "GitHubClient", lambda **kwargs: fake_client)
    monkeypatch.setattr(
        submit_script, "load_github_headers", lambda repo: ({"Authorization": "Bearer test-token"}, None)
    )
    monkeypatch.setattr(submit_script, "ensure_fork", lambda client, target, login: ("bot/MoviePilot", False))

    real_run_git = submit_script.run_git

    def run_git_with_fake_push(root_path: Path, *arguments: str, env=None):
        """让测试绕过网络 Push，但保留真实本地 Git 操作。"""
        if arguments and arguments[0] == "push":
            fake_client.remote_sha = common.git_output(root_path, "rev-parse", "HEAD").decode().strip()
            return subprocess.CompletedProcess(["git", *arguments], 0, b"", b"")
        return real_run_git(root_path, *arguments, env=env)

    @contextmanager
    def fake_git_auth(headers):
        """为离线测试提供不含凭据的 Git 环境。"""
        yield os.environ.copy()

    monkeypatch.setattr(submit_script, "run_git", run_git_with_fake_push)
    monkeypatch.setattr(submit_script, "git_auth_environment", fake_git_auth)

    result = submit_script.submit_pull_request(str(payload_file), "CONFIRM")

    assert result["success"] is True
    assert result["execution_outcome"] == "succeeded"
    assert result["pr_url"].endswith("/pull/1")
    assert _git(root, "log", "-1", "--pretty=%s") == "fix: test submit"
    assert json.loads(payload_file.read_text(encoding="utf-8"))["local_commit_sha"] == result["commit_sha"]
