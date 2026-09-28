"""在用户确认后通过本地 Git 推送分支并创建 Pull Request。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_common import (  # noqa: E402
    GitHubApiError,
    GitHubClient,
    PullRequestError,
    changes_from_payload,
    ensure_fork,
    git_auth_environment,
    git_current_branch,
    git_output,
    git_remote_url,
    has_github_auth,
    load_github_headers,
    normalize_branch,
    normalize_repo,
    pull_head,
    read_json_file,
    remote_repo_from_url,
    result_payload,
    run_git,
    write_json_file,
)


def _git_failure(result: Any, command: str, reason: str = "git_command_failed") -> PullRequestError:
    """将 Git 命令结果转换为不含凭据的 PR 错误。"""
    detail = result.stderr.decode("utf-8", errors="replace")[:500]
    return PullRequestError(reason, f"Git {command} 失败: {detail}")


def _status_clean(source_root: Path) -> bool:
    """判断本地 clone 是否没有未提交的工作树变更。"""
    result = run_git(source_root, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise _git_failure(result, "status")
    return not result.stdout


def _commit_changes(
    source_root: Path,
    changes: list[Any],
    commit_message: str,
) -> str:
    """只暂存预览中的文件并创建一个本地 Git Commit。"""
    paths = [change.path for change in changes]
    add_result = run_git(source_root, "add", "-A", "--", *paths)
    if add_result.returncode != 0:
        raise _git_failure(add_result, "add")
    commit_result = run_git(source_root, "commit", "--message", commit_message)
    if commit_result.returncode != 0:
        raise _git_failure(commit_result, "commit")
    return git_output(source_root, "rev-parse", "HEAD").decode().strip()


def _reuse_or_commit(
    payload_file: Path,
    payload: dict[str, Any],
    source_root: Path,
) -> str:
    """支持推送中断后的安全重试，不重复创建 Commit。"""
    existing_commit = str(payload.get("local_commit_sha") or "").strip()
    current_head = git_output(source_root, "rev-parse", "HEAD").decode().strip()
    if existing_commit:
        if current_head != existing_commit:
            raise PullRequestError("local_commit_changed", "本地 Commit 已偏离上次提交记录，请重新生成预览。")
        if not _status_clean(source_root):
            raise PullRequestError("changes_modified", "重试提交前发现工作树又有新的未提交变更。")
        return existing_commit

    initial_head = str(payload.get("initial_head_sha") or "").strip()
    if initial_head and current_head != initial_head:
        raise PullRequestError("local_commit_changed", "预览后的本地基线已改变，请重新生成预览。")
    changes = changes_from_payload(payload)
    commit_sha = _commit_changes(source_root, changes, str(payload["commit_message"]))
    payload["local_commit_sha"] = commit_sha
    write_json_file(payload_file, payload)
    return commit_sha


def _success_existing_pull(
    pull: dict[str, Any],
    *,
    target_repo: str,
    fork_repo: str,
    branch: str,
    base: str,
    commit_sha: str = "",
) -> dict[str, Any]:
    """统一构造已有 PR 的幂等成功结果。"""
    return {
        "success": True,
        "execution_outcome": "already_exists",
        "target_repo": target_repo,
        "fork_repo": fork_repo,
        "branch": branch,
        "base": base,
        "commit_sha": commit_sha or str((pull.get("head") or {}).get("sha") or ""),
        "pr_number": pull.get("number"),
        "pr_url": pull.get("html_url") or pull.get("url"),
        "draft": pull.get("draft"),
        "message": "该工作分支已有开放 PR，已复用现有 PR，未重复提交。",
    }


def submit_pull_request(payload_file: str, confirmation: str) -> dict[str, Any]:
    """重新校验预览后提交 Git Commit、推送分支并创建 PR。"""
    if confirmation != "CONFIRM":
        raise PullRequestError("confirmation_required", "提交 PR 必须传入精确确认标记 CONFIRM。")
    payload_path = Path(payload_file).expanduser().resolve()
    payload = read_json_file(payload_path, reason="invalid_payload")
    try:
        target_repo = normalize_repo(str(payload.get("target_repo") or ""))
        fork_repo = normalize_repo(str(payload.get("fork_repo") or ""))
        base = normalize_branch(str(payload.get("base") or ""))
        branch = normalize_branch(str(payload.get("branch") or ""))
    except ValueError as error:
        raise PullRequestError("invalid_payload", str(error)) from error
    source_root = Path(str(payload.get("source_root") or "")).expanduser().resolve()
    if branch == base:
        raise PullRequestError("base_branch_forbidden", "不能直接在 PR 基线分支上提交。")
    current_branch = git_current_branch(source_root)
    if current_branch != branch:
        raise PullRequestError("branch_mismatch", "当前 clone 分支与预览不一致。")
    origin_repo = remote_repo_from_url(git_remote_url(source_root, "origin"))
    if origin_repo.lower() != fork_repo.lower():
        raise PullRequestError("remote_mismatch", f"origin 未指向预览中的 Fork {fork_repo}。")

    headers, proxies = load_github_headers(target_repo)
    if not has_github_auth(headers):
        raise PullRequestError("no_token", "未配置可推送代码和创建 PR 的 GitHub Token。")
    client = GitHubClient(headers=headers, proxies=proxies)
    user = client.get_user()
    login = str(user.get("login") or "").strip()
    if not login:
        raise PullRequestError("invalid_github_user", "GitHub Token 未返回当前用户。")
    expected_login = str(payload.get("github_login") or "").strip()
    if expected_login and expected_login.lower() != login.lower():
        raise PullRequestError("github_identity_changed", "当前 GitHub Token 用户与预览不一致。")
    actual_fork, _ = ensure_fork(client, target_repo, login)
    if actual_fork.lower() != fork_repo.lower():
        raise PullRequestError("fork_mismatch", "当前用户的 Fork 与预览不一致。")

    base_ref = client.get_ref(target_repo, base)
    base_sha = str((base_ref.get("object") or {}).get("sha") or "")
    expected_base_sha = str(payload.get("expected_base_sha") or "")
    if not base_sha or (expected_base_sha and base_sha != expected_base_sha):
        raise PullRequestError("base_changed", "上游基线已变化，请重新 clone、修改并生成预览。")

    head = pull_head(target_repo, fork_repo, login, branch)
    existing_pulls = client.list_open_pulls(target_repo, head=head, base=base)
    if existing_pulls:
        return _success_existing_pull(
            existing_pulls[0],
            target_repo=target_repo,
            fork_repo=fork_repo,
            branch=branch,
            base=base,
        )

    remote_sha: Optional[str] = None
    try:
        remote_ref = client.get_ref(fork_repo, branch)
        remote_sha = str((remote_ref.get("object") or {}).get("sha") or "")
    except GitHubApiError as error:
        if error.status != 404:
            raise
    if remote_sha and not payload.get("local_commit_sha"):
        raise PullRequestError("branch_exists", f"Fork 中已存在未由本次流程创建的分支: {fork_repo}:{branch}")

    commit_sha = _reuse_or_commit(payload_path, payload, source_root)
    if remote_sha and remote_sha != commit_sha:
        raise PullRequestError("remote_branch_changed", "Fork 远端分支已指向其他 Commit，拒绝覆盖。")

    if not remote_sha:
        with git_auth_environment(headers) as git_environment:
            push_result = run_git(
                source_root,
                "push",
                "--set-upstream",
                "origin",
                branch,
                env=git_environment,
            )
        if push_result.returncode != 0:
            raise _git_failure(push_result, "push", reason="git_push_failed")

    verified_ref = client.get_ref(fork_repo, branch)
    verified_sha = str((verified_ref.get("object") or {}).get("sha") or "")
    if verified_sha != commit_sha:
        raise PullRequestError("push_verification_failed", "推送后的远端分支 SHA 与本地 Commit 不一致。")

    existing_pulls = client.list_open_pulls(target_repo, head=head, base=base)
    if existing_pulls:
        return _success_existing_pull(
            existing_pulls[0],
            target_repo=target_repo,
            fork_repo=fork_repo,
            branch=branch,
            base=base,
            commit_sha=commit_sha,
        )

    title = str(payload.get("title") or "").strip()
    if not title:
        raise PullRequestError("invalid_payload", "PR 标题不能为空。")
    pull = client.create_pull(
        target_repo,
        {
            "title": title,
            "head": head,
            "base": base,
            "body": str(payload.get("body") or ""),
            "draft": bool(payload.get("draft", True)),
        },
    )
    actual_base = str((pull.get("base") or {}).get("ref") or base)
    actual_head = str((pull.get("head") or {}).get("ref") or branch)
    if actual_base != base or actual_head != branch:
        raise PullRequestError("pull_verification_failed", "GitHub 返回的 PR head/base 与预期不一致。")
    return {
        "success": True,
        "execution_outcome": "succeeded",
        "target_repo": target_repo,
        "fork_repo": fork_repo,
        "branch": branch,
        "base": base,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "pr_number": pull.get("number"),
        "pr_url": pull.get("html_url") or pull.get("url"),
        "draft": bool(payload.get("draft", True)),
        "message": "已完成本地 Git Commit、推送 Fork 分支并创建 PR。",
    }


def _parse_args() -> argparse.Namespace:
    """解析 PR 提交脚本命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-file", required=True, help="prepare_pull_request.py 输出的 payload JSON")
    parser.add_argument("--confirm", required=True, help="必须是精确字符串 CONFIRM")
    return parser.parse_args()


def main() -> int:
    """执行 PR 提交入口并输出机器可解析 JSON。"""
    try:
        arguments = _parse_args()
        result = submit_pull_request(arguments.payload_file, arguments.confirm)
    except (GitHubApiError, PullRequestError, ValueError) as error:
        print(result_payload(success=False, reason=getattr(error, "reason", "invalid_request"), message=str(error)))
        return 2
    print(result_payload(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
