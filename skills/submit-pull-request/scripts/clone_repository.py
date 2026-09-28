"""为 PR 工作流确保 Fork，并从 Fork 创建一个隔离的 Git clone。"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_common import (  # noqa: E402
    GitHubApiError,
    GitHubClient,
    PullRequestError,
    build_branch_name,
    ensure_fork,
    git_auth_environment,
    git_output,
    github_clone_url,
    has_github_auth,
    load_github_headers,
    normalize_branch,
    normalize_repo,
    redact_text,
    result_payload,
    run_git,
    runtime_dir,
    write_json_file,
)


def _default_state_file(target_repo: str, destination: Path) -> Path:
    """为本次 clone 生成运行状态文件路径。"""
    digest = hashlib.sha256(f"{target_repo}:{destination}".encode("utf-8")).hexdigest()[:16]
    return runtime_dir() / "states" / f"{digest}.json"


def _resolve_destination(target_repo: str, destination: Optional[str]) -> Path:
    """解析并检查 clone 目标，拒绝覆盖已有非空目录。"""
    if destination:
        path = Path(destination).expanduser().resolve()
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise PullRequestError("destination_exists", f"clone 目标不是空目录: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return path
    owner, name = normalize_repo(target_repo).split("/", 1)
    clone_dir = runtime_dir() / "clones"
    clone_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{owner}-{name}-", dir=clone_dir))


def _run_checked_git(root: Path, *arguments: str, env: dict[str, str]) -> None:
    """运行 clone 流程中的 Git 命令并隐藏可能的凭据。"""
    result = run_git(root, *arguments, env=env)
    if result.returncode != 0:
        detail = redact_text(result.stderr.decode("utf-8", errors="replace"))[:500]
        raise PullRequestError("git_command_failed", f"Git 命令失败: {arguments[0]}: {detail}")


def clone_repository(
    target_repo: str,
    *,
    destination: Optional[str] = None,
    base: Optional[str] = None,
    branch: Optional[str] = None,
    state_file: Optional[str] = None,
) -> dict[str, Any]:
    """确保目标 Fork 存在，按上游基线创建隔离工作分支和状态文件。"""
    target = normalize_repo(target_repo)
    headers, proxies = load_github_headers(target)
    if not has_github_auth(headers):
        raise PullRequestError("no_token", "未配置可创建 Fork 和推送代码的 GitHub Token。")

    client = GitHubClient(headers=headers, proxies=proxies)
    user = client.get_user()
    login = str(user.get("login") or "").strip()
    if not login:
        raise PullRequestError("invalid_github_user", "GitHub Token 未返回当前用户。")
    upstream = client.get_repo(target)
    selected_base = normalize_branch(base or str(upstream.get("default_branch") or ""))
    base_ref = client.get_ref(target, selected_base)
    base_sha = str((base_ref.get("object") or {}).get("sha") or "")
    if not base_sha:
        raise PullRequestError("invalid_github_ref", f"无法读取上游基线: {target}:{selected_base}")

    fork_repo, fork_created = ensure_fork(client, target, login)
    selected_branch = normalize_branch(branch) if branch else build_branch_name(target, selected_base)
    if selected_branch == selected_base:
        raise PullRequestError("base_branch_forbidden", "工作分支不能与 PR 基线分支相同。")
    try:
        client.get_ref(fork_repo, selected_branch)
    except GitHubApiError as error:
        if error.status != 404:
            raise
    else:
        raise PullRequestError("branch_exists", f"Fork 中已存在工作分支: {fork_repo}:{selected_branch}")

    clone_path = _resolve_destination(target, destination)
    with git_auth_environment(headers) as git_environment:
        _run_checked_git(
            clone_path.parent,
            "clone",
            "--no-tags",
            github_clone_url(fork_repo),
            str(clone_path),
            env=git_environment,
        )
        _run_checked_git(
            clone_path,
            "remote",
            "add",
            "upstream",
            github_clone_url(target),
            env=git_environment,
        )
        _run_checked_git(
            clone_path,
            "fetch",
            "--no-tags",
            "upstream",
            selected_base,
            env=git_environment,
        )
        fetched_sha = git_output(clone_path, "rev-parse", "FETCH_HEAD", env=git_environment).decode().strip()
        if fetched_sha != base_sha:
            raise PullRequestError("base_changed", "上游基线在 clone 过程中发生变化，请重新开始。")
        _run_checked_git(
            clone_path,
            "switch",
            "--create",
            selected_branch,
            "FETCH_HEAD",
            env=git_environment,
        )

    state_path = Path(state_file).expanduser().resolve() if state_file else _default_state_file(target, clone_path)
    if state_path == clone_path or clone_path in state_path.parents:
        raise PullRequestError("invalid_state", "state_file 必须位于 clone 目录之外。")
    state = {
        "version": 1,
        "target_repo": target,
        "fork_repo": fork_repo,
        "source_root": str(clone_path),
        "base": selected_base,
        "expected_base_sha": base_sha,
        "branch": selected_branch,
        "origin_remote": "origin",
        "upstream_remote": "upstream",
        "fork_created": fork_created,
    }
    write_json_file(state_path, state)
    return {
        "success": True,
        "target_repo": target,
        "fork_repo": fork_repo,
        "fork_created": fork_created,
        "source_root": str(clone_path),
        "state_file": str(state_path),
        "base": selected_base,
        "base_sha": base_sha,
        "branch": selected_branch,
        "message": "已完成 Fork 复用/创建，并从上游基线创建隔离 Git 工作分支。",
    }


def _parse_args() -> argparse.Namespace:
    """解析 clone 脚本命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", required=True, help="上游 GitHub owner/repo")
    parser.add_argument("--destination", help="clone 目标目录；必须不存在或为空")
    parser.add_argument("--base", help="PR 基线分支；默认读取上游 default_branch")
    parser.add_argument("--branch", help="工作分支；默认生成隔离分支")
    parser.add_argument("--state-file", help="将 clone 状态写入指定 JSON 文件")
    return parser.parse_args()


def main() -> int:
    """执行 clone 入口并输出机器可解析 JSON。"""
    try:
        arguments = _parse_args()
        result = clone_repository(
            arguments.target_repo,
            destination=arguments.destination,
            base=arguments.base,
            branch=arguments.branch,
            state_file=arguments.state_file,
        )
    except (GitHubApiError, PullRequestError, ValueError) as error:
        print(result_payload(success=False, reason=getattr(error, "reason", "invalid_request"), message=str(error)))
        return 2
    print(result_payload(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
