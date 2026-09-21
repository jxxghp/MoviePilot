"""校验 Git clone 的变更并生成需要用户确认的 PR 预览。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_common import (  # noqa: E402
    GitHubApiError,
    GitHubClient,
    PullRequestError,
    changes_from_payload,
    ensure_fork,
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
    runtime_dir,
    write_json_file,
)


def _text_field(draft: dict[str, Any], name: str, *, required: bool = True, limit: int = 100_000) -> str:
    """读取并限制草稿文本字段，避免把超大内容送入 GitHub API。"""
    value = draft.get(name, "")
    if not isinstance(value, str):
        raise PullRequestError("invalid_draft", f"草稿字段 {name} 必须是字符串。")
    value = value.strip()
    if required and not value:
        raise PullRequestError("invalid_draft", f"草稿缺少 {name}。")
    if len(value) > limit:
        raise PullRequestError("invalid_draft", f"草稿字段 {name} 过长。")
    return value


def _read_changes_metadata(changes_file: Path) -> dict[str, Any]:
    """读取采集脚本生成的变更摘要元数据。"""
    data = read_json_file(changes_file, reason="invalid_changes")
    if not isinstance(data.get("fingerprint"), str) or not data.get("fingerprint"):
        raise PullRequestError("invalid_changes", "变更摘要缺少指纹。")
    return data


def _preview_text(
    *,
    target_repo: str,
    fork_repo: str,
    base: str,
    branch: str,
    changes: list[Any],
    title: str,
    commit_message: str,
    body: str,
    draft: bool,
    existing_pull: list[dict[str, Any]],
) -> str:
    """生成可直接展示给用户的完整 PR 预览。"""
    lines = [
        "# MoviePilot Agent PR 预览",
        "",
        f"- 上游仓库：`{target_repo}`",
        f"- Fork：`{fork_repo}`（复用已有 Fork；如不存在，clone 阶段已创建）",
        f"- 基线分支：`{base}`",
        f"- 工作分支：`{branch}`",
        f"- PR 类型：{'Draft' if draft else 'Ready for review'}",
        "- 提交方式：本地 Git `add` → `commit` → `push`，成功后通过 GitHub API 创建 PR",
        "",
        "## 文件变更",
        "",
    ]
    for change in changes:
        lines.append(f"- `{change.operation}` `{change.path}` ({change.size_bytes} bytes, mode {change.mode})")
    lines.extend(
        [
            "",
            "## Commit message",
            "",
            commit_message,
            "",
            "## PR 标题",
            "",
            title,
            "",
            "## PR 正文",
            "",
            body or "（空）",
            "",
            "## 幂等提示",
            "",
            "提交脚本会重新校验工作树摘要、上游基线、Fork remote 和推送后的分支 SHA；不执行强制推送或合并。",
        ]
    )
    if existing_pull:
        lines.extend(["", "## 已存在的开放 PR", ""])
        for pull in existing_pull:
            lines.append(f"- {pull.get('html_url') or pull.get('url') or '（无 URL）'}")
    return "\n".join(lines) + "\n"


def prepare_pull_request(draft_file: str) -> dict[str, Any]:
    """校验草稿和 Git 工作树，生成 payload 与完整预览文件。"""
    draft_path = Path(draft_file).expanduser().resolve()
    draft = read_json_file(draft_path, reason="invalid_draft")
    try:
        target_repo = normalize_repo(str(draft.get("target_repo") or ""))
    except ValueError as error:
        raise PullRequestError("invalid_draft", str(error)) from error

    source_root = Path(str(draft.get("source_root") or "")).expanduser().resolve()
    changes_file = Path(str(draft.get("changes_file") or "")).expanduser().resolve()
    draft_for_changes = {"source_root": str(source_root), "changes_file": str(changes_file)}
    changes = changes_from_payload(
        {
            **draft_for_changes,
            "fingerprint": _read_changes_metadata(changes_file).get("fingerprint"),
        }
    )
    branch = git_current_branch(source_root)
    requested_branch = draft.get("branch")
    if requested_branch:
        try:
            requested_branch = normalize_branch(str(requested_branch))
        except ValueError as error:
            raise PullRequestError("invalid_draft", str(error)) from error
        if requested_branch != branch:
            raise PullRequestError("branch_mismatch", "草稿分支与当前 clone 工作分支不一致。")

    state: dict[str, Any] = {}
    if draft.get("state_file"):
        state = read_json_file(Path(str(draft["state_file"])).expanduser().resolve())
        if Path(str(state.get("source_root") or "")).expanduser().resolve() != source_root:
            raise PullRequestError("state_mismatch", "clone 状态文件与 source_root 不匹配。")
        if state.get("target_repo", "").lower() != target_repo.lower():
            raise PullRequestError("state_mismatch", "clone 状态文件与目标仓库不匹配。")

    headers, proxies = load_github_headers(target_repo)
    if not has_github_auth(headers):
        raise PullRequestError("no_token", "未配置可读取仓库并提交 PR 的 GitHub Token。")
    client = GitHubClient(headers=headers, proxies=proxies)
    user = client.get_user()
    login = str(user.get("login") or "").strip()
    if not login:
        raise PullRequestError("invalid_github_user", "GitHub Token 未返回当前用户。")
    upstream = client.get_repo(target_repo)
    base = str(draft.get("base") or state.get("base") or upstream.get("default_branch") or "").strip()
    try:
        base = normalize_branch(base)
    except ValueError as error:
        raise PullRequestError("invalid_draft", str(error)) from error
    base_ref = client.get_ref(target_repo, base)
    base_sha = str((base_ref.get("object") or {}).get("sha") or "")
    if not base_sha:
        raise PullRequestError("invalid_github_ref", f"无法读取上游基线: {target_repo}:{base}")
    expected_state_sha = str(state.get("expected_base_sha") or draft.get("expected_base_sha") or "")
    if expected_state_sha and expected_state_sha != base_sha:
        raise PullRequestError("base_changed", "上游基线已变化，请重新 clone 后再修改代码。")
    initial_head_sha = git_output(source_root, "rev-parse", "HEAD").decode().strip()
    if initial_head_sha != base_sha:
        raise PullRequestError(
            "local_base_mismatch",
            "本地 clone 的工作分支不是当前上游基线，请使用 clone_repository.py 重新准备。",
        )
    if branch == base:
        raise PullRequestError("base_branch_forbidden", "不能直接在 PR 基线分支上提交。")

    fork_repo, fork_created = ensure_fork(client, target_repo, login)
    origin_repo = remote_repo_from_url(git_remote_url(source_root, "origin"))
    if origin_repo.lower() != fork_repo.lower():
        raise PullRequestError(
            "remote_mismatch",
            f"origin 必须指向当前用户 Fork {fork_repo}，实际为 {origin_repo}。",
        )
    try:
        client.get_ref(fork_repo, branch)
    except GitHubApiError as error:
        if error.status != 404:
            raise
    else:
        raise PullRequestError("branch_exists", f"Fork 中已存在工作分支: {fork_repo}:{branch}")

    title = _text_field(draft, "title", limit=256)
    commit_message = _text_field(draft, "commit_message", limit=256)
    body = _text_field(draft, "body", required=False)
    draft_pr = draft.get("draft", True)
    if not isinstance(draft_pr, bool):
        raise PullRequestError("invalid_draft", "草稿字段 draft 必须是布尔值。")

    head = pull_head(target_repo, fork_repo, login, branch)
    existing_pull = client.list_open_pulls(target_repo, head=head, base=base)
    changes_metadata = _read_changes_metadata(changes_file)
    payload = {
        "version": 1,
        "target_repo": target_repo,
        "fork_repo": fork_repo,
        "source_root": str(source_root),
        "state_file": str(Path(str(draft["state_file"])).expanduser().resolve()) if draft.get("state_file") else "",
        "changes_file": str(changes_file),
        "change_fingerprint": changes_metadata["fingerprint"],
        "base": base,
        "expected_base_sha": base_sha,
        "initial_head_sha": initial_head_sha,
        "branch": branch,
        "github_login": login,
        "head": head,
        "title": title,
        "body": body,
        "commit_message": commit_message,
        "draft": draft_pr,
        "fork_created": fork_created,
    }
    payload_digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    payload_file = runtime_dir() / "payloads" / f"{payload_digest}.json"
    preview_file = runtime_dir() / "previews" / f"{payload_digest}.md"
    write_json_file(payload_file, payload)
    preview_file.parent.mkdir(parents=True, exist_ok=True)
    preview_file.write_text(
        _preview_text(
            target_repo=target_repo,
            fork_repo=fork_repo,
            base=base,
            branch=branch,
            changes=changes,
            title=title,
            commit_message=commit_message,
            body=body,
            draft=draft_pr,
            existing_pull=existing_pull,
        ),
        encoding="utf-8",
    )
    return {
        "success": True,
        "payload_file": str(payload_file),
        "preview_file": str(preview_file),
        "target_repo": target_repo,
        "fork_repo": fork_repo,
        "base": base,
        "base_sha": base_sha,
        "branch": branch,
        "change_count": len(changes),
        "existing_pull_requests": existing_pull,
        "message": "预览已生成；请向用户展示 preview_file 全文并等待明确确认。",
    }


def _parse_args() -> argparse.Namespace:
    """解析 PR 预览脚本命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-file", required=True, help="Agent 写入 runtime_dir 的 PR 草稿 JSON")
    return parser.parse_args()


def main() -> int:
    """执行 PR 预览入口并输出机器可解析 JSON。"""
    try:
        arguments = _parse_args()
        result = prepare_pull_request(arguments.draft_file)
    except (GitHubApiError, PullRequestError, ValueError) as error:
        print(result_payload(success=False, reason=getattr(error, "reason", "invalid_request"), message=str(error)))
        return 2
    print(result_payload(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
