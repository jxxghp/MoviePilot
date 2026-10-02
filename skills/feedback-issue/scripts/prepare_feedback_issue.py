"""校验并生成 feedback-issue 提交前的预览与 payload 文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from feedback_issue_common import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ISSUE_TYPES,
    FEEDBACK_REPO,
    MAX_PREVIEW_LOGS_CHARS,
    MAX_TITLE_CHARS,
    build_issue_body,
    check_content_quality,
    format_doctor_summary,
    format_log_selection,
    load_diagnostics_logs,
    normalize_target_repo,
    read_json_file,
    result_payload,
    runtime_file,
    sanitize_logs,
    truncate,
    validate_enum,
    validate_target_repo_for_issue,
    write_json_file,
)


REQUIRED_DRAFT_FIELDS = (
    "title",
    "version",
    "environment",
    "issue_type",
    "description",
    "original_user_request",
    "diagnostics_file",
)


def normalize_draft(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """规范化草稿字段并返回缺失字段列表。"""
    draft = {key: str(raw.get(key) or "").strip() for key in REQUIRED_DRAFT_FIELDS}
    missing = [key for key, value in draft.items() if not value]
    draft["title"] = truncate(draft["title"], MAX_TITLE_CHARS, marker="...")
    draft["target_repo"] = str(raw.get("target_repo") or FEEDBACK_REPO).strip()
    return draft, missing


def validate_draft(draft: dict[str, Any], logs: str) -> Optional[str]:
    """校验草稿枚举和内容质量，返回错误信息或 None。"""
    for value, allowed, field_name in (
        (draft["environment"], ALLOWED_ENVIRONMENTS, "environment"),
        (draft["issue_type"], ALLOWED_ISSUE_TYPES, "issue_type"),
    ):
        error = validate_enum(value, allowed, field_name)
        if error:
            return error
    repo_error = validate_target_repo_for_issue(draft["issue_type"], draft["target_repo"])
    if repo_error:
        return repo_error
    return check_content_quality(
        title=draft["title"],
        description=draft["description"],
        original_user_request=draft["original_user_request"],
        logs=logs,
        issue_type=draft["issue_type"],
    )


def build_preview_text(draft: dict[str, Any], logs: str, diagnostics: dict[str, Any]) -> str:
    """构造给用户确认的 Markdown 预览文本。"""
    preview_logs = sanitize_logs(logs, MAX_PREVIEW_LOGS_CHARS) or "会话中未捕获到相关后端日志。"
    doctor_summary = format_doctor_summary(diagnostics.get("doctor"))
    log_selection_summary = format_log_selection(diagnostics.get("log_selection"))
    source_files = diagnostics.get("source_files") or []
    sources = "\n".join(f"- {item}" for item in source_files) or "- 未命中具体日志文件"
    return (
        "请确认是否提交以下问题反馈：\n\n"
        f"标题：{draft['title']}\n"
        f"目标仓库：{draft['target_repo']}\n"
        f"版本：{draft['version']}\n"
        f"环境：{draft['environment']}\n"
        f"类型：{draft['issue_type']}\n\n"
        "诊断来源：\n"
        f"{sources}\n\n"
        "Doctor 摘要：\n"
        f"```text\n{doctor_summary}\n```\n\n"
        "日志筛选依据：\n"
        f"```text\n{log_selection_summary}\n```\n\n"
        "问题描述：\n"
        f"{draft['description'].strip()}\n\n"
        "日志预览（已脱敏）：\n"
        f"```bash\n{preview_logs}\n```\n\n"
        "请核对目标仓库、问题描述与脱敏日志，再选择确认提交、修改内容或取消。"
    )


def prepare_issue(draft_file: str | Path) -> dict[str, Any]:
    """读取草稿 JSON，校验后写出 payload 与 preview 文件。"""
    raw = read_json_file(draft_file)
    draft, missing = normalize_draft(raw)
    if missing:
        return {
            "success": False,
            "reason": "missing_fields",
            "message": f"草稿缺少必填字段：{', '.join(missing)}",
        }
    try:
        draft["target_repo"] = normalize_target_repo(draft["target_repo"])
    except ValueError as err:
        return {
            "success": False,
            "reason": "invalid_target_repo",
            "message": str(err),
        }

    try:
        logs, diagnostics = load_diagnostics_logs(draft["diagnostics_file"])
    except Exception as err:
        return {
            "success": False,
            "reason": "diagnostics_missing",
            "message": f"无法读取诊断日志文件：{err}",
        }

    error = validate_draft(draft, logs)
    if error:
        return {
            "success": False,
            "reason": "invalid_draft",
            "message": error,
        }

    payload = {
        **draft,
        "diagnostics_file": str(draft["diagnostics_file"]),
    }
    payload_file = runtime_file("payload", ".json")
    preview_file = runtime_file("preview", ".md")
    write_json_file(payload_file, payload)
    preview_text = build_preview_text(draft, logs, diagnostics)
    preview_file.write_text(preview_text, encoding="utf-8")

    combined_logs = "\n\n".join(
        part for part in (
            f"### Doctor 摘要\n{format_doctor_summary(diagnostics.get('doctor'))}",
            f"### 日志筛选依据\n{format_log_selection(diagnostics.get('log_selection'))}",
            logs,
        ) if part
    )
    body_preview = build_issue_body(
        version=draft["version"],
        environment=draft["environment"],
        issue_type=draft["issue_type"],
        description=draft["description"],
        logs=combined_logs,
        target_repo=draft["target_repo"],
    )
    return {
        "success": True,
        "target_repo": draft["target_repo"],
        "payload_file": str(payload_file),
        "preview_file": str(preview_file),
        "confirmation_options": [
            {"label": "确认提交", "value": "确认提交当前预览的 Issue"},
            {"label": "修改内容", "value": "修改当前 Issue 草稿"},
            {"label": "取消", "value": "取消本次 Issue 反馈"},
        ],
        "body_chars": len(body_preview),
        "log_bytes": len(logs.encode("utf-8", errors="replace")),
        "log_lines": len(logs.splitlines()) if logs else 0,
        "message": (
            "已生成 Issue 预览和提交 payload。支持按钮时，用 ask_user_choice 将 preview_file 全文"
            "作为 message、confirmation_options 作为 options 展示并结束本轮；否则使用文字确认。"
            "用户点击确认提交或文字确认当前预览后，才能调用 submit_feedback_issue.py；"
            "修改后须重新预览并确认，取消则不提交。"
        ),
    }


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="生成 MoviePilot 反馈 Issue 预览")
    parser.add_argument("--draft-file", required=True, help="包含 Issue 草稿字段的 JSON 文件")
    return parser.parse_args()


def main() -> int:
    """脚本入口：校验草稿并输出 JSON 结果。"""
    args = parse_args()
    result = prepare_issue(args.draft_file)
    print(result_payload(**result))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
