"""提交 feedback-issue payload 到目标 GitHub 仓库。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from feedback_issue_common import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ISSUE_TYPES,
    FEEDBACK_REPO,
    FEEDBACK_REQUEST_TIMEOUT,
    MAX_TITLE_CHARS,
    build_issue_body,
    build_prefill_url,
    check_content_quality,
    check_recent_duplicate,
    check_user_rate_limit,
    classify_failure,
    format_doctor_summary,
    format_log_selection,
    issue_api_url,
    issue_labels,
    load_diagnostics_logs,
    load_submission_state,
    normalize_target_repo,
    read_json_file,
    record_submission,
    record_user_submission,
    result_payload,
    safe_response_dict,
    save_submission_state,
    settings,
    truncate,
    validate_enum,
    validate_target_repo_for_issue,
)
from app.adapters.network.http import RequestUtils


REQUIRED_PAYLOAD_FIELDS = (
    "title",
    "version",
    "environment",
    "issue_type",
    "description",
    "original_user_request",
    "diagnostics_file",
)


def normalize_payload(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """规范化提交 payload 并返回缺失字段。"""
    payload = {key: str(raw.get(key) or "").strip() for key in REQUIRED_PAYLOAD_FIELDS}
    missing = [key for key, value in payload.items() if not value]
    payload["title"] = truncate(payload["title"], MAX_TITLE_CHARS, marker="...")
    payload["target_repo"] = str(raw.get("target_repo") or FEEDBACK_REPO).strip()
    return payload, missing


def validate_payload(payload: dict[str, Any], logs: str) -> Optional[str]:
    """校验提交 payload 的枚举值和内容质量。"""
    for value, allowed, field_name in (
        (payload["environment"], ALLOWED_ENVIRONMENTS, "environment"),
        (payload["issue_type"], ALLOWED_ISSUE_TYPES, "issue_type"),
    ):
        error = validate_enum(value, allowed, field_name)
        if error:
            return error
    repo_error = validate_target_repo_for_issue(payload["issue_type"], payload["target_repo"])
    if repo_error:
        return repo_error
    return check_content_quality(
        title=payload["title"],
        description=payload["description"],
        original_user_request=payload["original_user_request"],
        logs=logs,
        issue_type=payload["issue_type"],
    )


def build_no_token_result(payload: dict[str, Any], logs: str) -> dict[str, Any]:
    """构造未配置 GitHub Token 时的预填链接降级结果。"""
    prefill_url = build_prefill_url(
        title=payload["title"],
        version=payload["version"],
        environment=payload["environment"],
        issue_type=payload["issue_type"],
        description=payload["description"],
        logs=logs,
        target_repo=payload["target_repo"],
    )
    return {
        "success": False,
        "reason": "no_token",
        "repo": payload["target_repo"],
        "prefill_url": prefill_url,
        "message": (
            "MoviePilot 未配置可写入的 GitHub Token，无法自动提交 Issue。"
            "请把 prefill_url 原样发给用户，由用户在浏览器或 GitHub App 中确认提交。"
        ),
    }


def post_github_issue(payload: dict[str, Any], body: str) -> Any:
    """调用 GitHub REST API 创建 Issue 并返回响应对象。"""
    request_headers = {
        **settings.GITHUB_HEADERS,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    request_payload = {
        "title": payload["title"],
        "body": body,
    }
    labels = issue_labels(payload["issue_type"], payload["target_repo"])
    if labels:
        request_payload["labels"] = labels
    return RequestUtils(
        proxies=settings.PROXY,
        headers=request_headers,
        timeout=FEEDBACK_REQUEST_TIMEOUT,
    ).post(issue_api_url(payload["target_repo"]), json=request_payload)


def build_api_failure_result(
    *,
    reason: str,
    payload: dict[str, Any],
    logs: str,
    github_message: str | None = None,
) -> dict[str, Any]:
    """构造 GitHub API 失败后的预填链接兜底结果。"""
    prefill_url = build_prefill_url(
        title=payload["title"],
        version=payload["version"],
        environment=payload["environment"],
        issue_type=payload["issue_type"],
        description=payload["description"],
        logs=logs,
        target_repo=payload["target_repo"],
    )
    return {
        "success": False,
        "reason": reason,
        "repo": payload["target_repo"],
        "prefill_url": prefill_url,
        "github_message": github_message,
        "message": "GitHub API 未能自动创建 Issue，请把 prefill_url 原样发给用户手动提交。",
    }


def submit_issue(payload_file: str | Path, username: str) -> dict[str, Any]:
    """读取 payload 文件并执行提交或预填链接降级流程。"""
    raw = read_json_file(payload_file)
    payload, missing = normalize_payload(raw)
    if missing:
        return {
            "success": False,
            "reason": "missing_fields",
            "message": f"payload 缺少必填字段：{', '.join(missing)}",
        }
    try:
        payload["target_repo"] = normalize_target_repo(payload["target_repo"])
    except ValueError as err:
        return {
            "success": False,
            "reason": "invalid_target_repo",
            "message": str(err),
        }

    try:
        logs, diagnostics = load_diagnostics_logs(payload["diagnostics_file"])
    except Exception as err:
        return {
            "success": False,
            "reason": "diagnostics_missing",
            "message": f"无法读取诊断日志文件：{err}",
        }

    error = validate_payload(payload, logs)
    if error:
        return {
            "success": False,
            "reason": "rejected_quality",
            "message": error,
        }

    combined_logs = "\n\n".join(
        part for part in (
            f"### Doctor 摘要\n{format_doctor_summary(diagnostics.get('doctor'))}",
            f"### 日志筛选依据\n{format_log_selection(diagnostics.get('log_selection'))}",
            logs,
        ) if part
    )
    body = build_issue_body(
        version=payload["version"],
        environment=payload["environment"],
        issue_type=payload["issue_type"],
        description=payload["description"],
        logs=combined_logs,
        target_repo=payload["target_repo"],
    )
    state = load_submission_state()
    if check_recent_duplicate(payload["title"], body, state, payload["target_repo"]):
        return {
            "success": False,
            "reason": "duplicate",
            "message": "该问题反馈在 60 秒内已经提交或尝试提交过一次，已避免重复提交。",
        }

    rate_error = check_user_rate_limit(username, state)
    if rate_error:
        result = build_api_failure_result(
            reason="rate_limited_user",
            payload=payload,
            logs=combined_logs,
        )
        result["message"] = rate_error + " 如确实是另一个真实问题，请使用 prefill_url 手动提交。"
        save_submission_state(state)
        return result

    record_user_submission(username, state)
    if not settings.GITHUB_TOKEN:
        save_submission_state(state)
        return build_no_token_result(payload, combined_logs)

    record_submission(payload["title"], body, state, payload["target_repo"])
    save_submission_state(state)
    try:
        response = post_github_issue(payload, body)
    except Exception as err:
        return build_api_failure_result(
            reason="network_error",
            payload=payload,
            logs=combined_logs,
            github_message=str(err),
        )

    if response is None:
        return build_api_failure_result(
            reason="network_error",
            payload=payload,
            logs=combined_logs,
        )

    if response.status_code == 201:
        data = safe_response_dict(response)
        return {
            "success": True,
            "repo": payload["target_repo"],
            "issue_number": data.get("number"),
            "issue_url": data.get("html_url"),
            "message": f"Issue 已成功提交到 {payload['target_repo']} 仓库。",
        }

    reason = classify_failure(response.status_code, headers=dict(response.headers or {}))
    api_data = safe_response_dict(response)
    api_message = api_data.get("message") if api_data else None
    if not api_message and getattr(response, "text", None):
        api_message = response.text[:200]
    return build_api_failure_result(
        reason=reason,
        payload=payload,
        logs=combined_logs,
        github_message=api_message,
    )


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="提交 MoviePilot 反馈 Issue")
    parser.add_argument("--payload-file", required=True, help="prepare 脚本生成的 payload JSON 文件")
    parser.add_argument(
        "--username",
        default="agent-admin",
        help="用于提交频率限制的管理员用户名；未知时保留默认值",
    )
    return parser.parse_args()


def main() -> int:
    """脚本入口：输出 JSON 提交结果。"""
    args = parse_args()
    result = submit_issue(args.payload_file, args.username)
    print(result_payload(**result))
    return 0 if result.get("success") or result.get("reason") in {"no_token"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
