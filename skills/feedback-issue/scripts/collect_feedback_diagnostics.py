"""收集 feedback-issue 提交流程需要的本地诊断日志。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from feedback_issue_common import (
    MAX_LOGS_CHARS,
    format_log_selection,
    feedback_runtime_dir,
    result_payload,
    runtime_file,
    sanitize_logs,
    settings,
    write_json_file,
)

from app.runtime.log import PLUGIN_LOG_FILENAME  # noqa: E402


_MAX_READ_BYTES = 512 * 1024
_DEFAULT_TIME_WINDOW_MINUTES = 30
_MIN_TIME_WINDOW_MINUTES = 5
_MAX_TIME_WINDOW_MINUTES = 24 * 60

_LOG_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})")
_LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_MODULE_RE = re.compile(
    r"^【[^】]+】\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+\s+-\s+([^\s][^\-]*?)\s+-\s+"
)
_LOG_DYNAMIC_VALUE_RE = re.compile(
    r"(?<![A-Za-z])(?:[0-9a-f]{8,}|\d+(?:\.\d+)?)(?![A-Za-z])",
    re.IGNORECASE,
)

_META_NOISE_MODULES = frozenset({
    "collect_feedback_diagnostics.py",
    "prepare_feedback_issue.py",
    "submit_feedback_issue.py",
    "ask_user_choice.py",
    "base.py",
    "agent",
    "factory.py",
    "callback",
    "prompt",
    "memory.py",
    "activity_log.py",
    "message.py",
    "event.py",
    "chain",
    "discord",
    "telegram",
    "telegram.py",
    "execute_command.py",
})

_VAGUE_KEYWORDS = frozenset({
    "错误", "异常", "失败", "error", "exception", "failed", "warn", "warning",
    "日志", "问题", "bug", "log", "logs",
})

_FEEDBACK_VERB_PHRASES: tuple[str, ...] = (
    "反馈", "提交", "上报", "汇报",
    "提 issue", "提issue", "提 bug", "提bug",
    "提需求", "提交需求", "反馈需求", "提功能", "功能请求",
    "报 bug", "报bug", "报告 bug", "报告bug",
    "新建 issue", "新建issue", "开 issue", "开issue",
    "让上游", "给上游",
    "file an issue", "report a bug", "open an upstream issue",
    "submit an issue", "raise an issue", "report this upstream",
    "report upstream", "feature request", "submit a feature request",
    "open a feature request",
)
_FEEDBACK_TARGET_TOKENS: tuple[str, ...] = (
    "issue", "bug", "问题", "错误报告",
    "上游", "mp", "moviepilot", "需求", "功能", "feature",
)
_FEEDBACK_STANDALONE_PHRASES: tuple[str, ...] = (
    "file an issue", "report a bug", "open an upstream issue",
    "submit an issue", "raise an issue", "report this upstream",
    "report upstream", "feature request", "submit a feature request",
    "open a feature request",
    "新建 issue", "新建issue", "开 issue", "开issue",
    "提 issue", "提issue", "提 bug", "提bug",
    "提需求", "提交需求", "反馈需求", "提功能请求", "功能请求",
    "报 bug", "报bug", "报告 bug", "报告bug",
    "让上游", "给上游",
)
_FEEDBACK_REGEX_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"提.{0,6}(bug|issue|问题|错误报告)", re.IGNORECASE),
    re.compile(r"提.{0,6}(需求|功能请求|feature request)", re.IGNORECASE),
    re.compile(r"提交.{0,6}(需求|功能请求|feature request)", re.IGNORECASE),
    re.compile(r"报.{0,6}(bug|issue|错误报告)", re.IGNORECASE),
    re.compile(r"反馈.{0,8}(issue|bug|问题|上游|错误)", re.IGNORECASE),
    re.compile(r"反馈.{0,8}(需求|功能请求|feature request)", re.IGNORECASE),
    re.compile(r"开.{0,4}(issue|bug)", re.IGNORECASE),
    re.compile(r"开.{0,8}(需求|功能请求|feature request)", re.IGNORECASE),
    re.compile(r"上报.{0,6}(bug|issue|问题|错误)", re.IGNORECASE),
)


def read_tail(path: Path) -> str:
    """读取日志文件尾部，避免大日志一次性进入内存。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as file_obj:
            if size > _MAX_READ_BYTES:
                file_obj.seek(size - _MAX_READ_BYTES)
            return file_obj.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def candidate_log_files() -> list[Path]:
    """返回反馈诊断可读取的主日志和插件日志文件。

    插件日志同时扫描旧版扁平布局（`settings.LOG_PATH/plugins/<插件id>.log*`，
    不迁移、仍留在磁盘上）和新版按实例分目录的布局
    （`settings.PLUGIN_DATA_PATH/<插件id>/<实例id>/logs/plugin.log*`），
    避免仅适配新位置导致看不到新写入的插件日志。
    """
    files = [settings.LOG_PATH / "moviepilot.log"]
    legacy_plugin_log_dir = settings.LOG_PATH / "plugins"
    if legacy_plugin_log_dir.exists():
        files.extend(sorted(legacy_plugin_log_dir.rglob("*.log")))
    new_plugin_log_root = settings.PLUGIN_DATA_PATH
    if new_plugin_log_root.exists():
        files.extend(sorted(new_plugin_log_root.rglob(f"{PLUGIN_LOG_FILENAME}*")))
    return [path for path in files if path.exists() and path.is_file()]


def collect_doctor_report() -> dict:
    """调用离线 doctor 命令收集结构化诊断报告。"""
    commands = []
    moviepilot_bin = shutil.which("moviepilot")
    if moviepilot_bin:
        commands.append([moviepilot_bin, "doctor", "--json"])
    commands.append([sys.executable, "-m", "app.cli", "doctor", "--json"])

    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=str(settings.ROOT_PATH),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            last_error = str(err)
            continue

        output = (result.stdout or "").strip()
        if not output:
            last_error = f"{' '.join(command)} 没有输出"
            continue
        try:
            payload = json_loads_from_output(output)
        except ValueError as err:
            last_error = str(err)
            continue
        payload["_command"] = " ".join(command)
        payload["_returncode"] = result.returncode
        return {
            "success": True,
            "report": payload,
        }

    return {
        "success": False,
        "error": last_error if "last_error" in locals() else "doctor 命令不可用",
    }


def json_loads_from_output(output: str) -> dict:
    """从命令输出中解析 doctor JSON 对象。"""
    import json

    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("doctor 输出中未找到 JSON 对象")
    payload = json.loads(output[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("doctor JSON 顶层不是对象")
    return payload


def normalize_keywords(keywords: Optional[list[str]]) -> list[str]:
    """过滤掉过短或过于宽泛的日志关键词。"""
    normalized: list[str] = []
    for item in keywords or []:
        item = str(item or "").strip()
        if len(item) < 2:
            continue
        if item.lower() in _VAGUE_KEYWORDS:
            continue
        if item not in normalized:
            normalized.append(item)
    return normalized


def has_explicit_feedback_intent(original_user_request: str) -> bool:
    """判断用户原话里是否出现明确要求提 Issue 的意图。"""
    if not original_user_request:
        return False
    normalized = original_user_request.lower().strip()
    if any(phrase in normalized for phrase in _FEEDBACK_STANDALONE_PHRASES):
        return True
    if any(pattern.search(normalized) for pattern in _FEEDBACK_REGEX_PATTERNS):
        return True
    has_verb = any(phrase in normalized for phrase in _FEEDBACK_VERB_PHRASES)
    has_target = any(token in normalized for token in _FEEDBACK_TARGET_TOKENS)
    return has_verb and has_target


def normalize_window(time_window_minutes: int) -> int:
    """把传入的时间窗限制到 5 到 1440 分钟之间。"""
    try:
        window = int(time_window_minutes or _DEFAULT_TIME_WINDOW_MINUTES)
    except (TypeError, ValueError):
        window = _DEFAULT_TIME_WINDOW_MINUTES
    return max(_MIN_TIME_WINDOW_MINUTES, min(_MAX_TIME_WINDOW_MINUTES, window))


def parse_line_timestamp(line: str) -> Optional[datetime]:
    """从一行日志开头提取时间戳；提取不到返回 None。"""
    match = _LOG_TIMESTAMP_RE.search(line[:64])
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _LOG_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def is_meta_noise(line: str) -> bool:
    """判断日志行是否来自 Agent 自身的工具调度或消息框架噪音。"""
    match = _LOG_MODULE_RE.match(line)
    if not match:
        return False
    return match.group(1).strip() in _META_NOISE_MODULES


def _repetition_fingerprint(line: str) -> str:
    """生成用于识别连续重复日志模板的指纹。"""
    if parse_line_timestamp(line) is None:
        return line
    normalized = _LOG_TIMESTAMP_RE.sub("<time>", line)
    normalized = _LOG_DYNAMIC_VALUE_RE.sub("<value>", normalized)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _compact_repeated_lines(lines: list[str]) -> list[str]:
    """压缩连续重复日志模板，同时保留首条、末条和重复次数。"""
    compacted: list[str] = []
    group: list[str] = []
    fingerprint: Optional[str] = None

    def flush_group() -> None:
        if len(group) < 4:
            compacted.extend(group)
            return
        compacted.append(group[0])
        compacted.append(
            f"... 同类日志连续重复 {len(group)} 次，已省略 {len(group) - 2} 行 ..."
        )
        compacted.append(group[-1])

    for line in lines:
        current_fingerprint = _repetition_fingerprint(line)
        if group and current_fingerprint != fingerprint:
            flush_group()
            group = []
        group.append(line)
        fingerprint = current_fingerprint
    if group:
        flush_group()
    return compacted


def filter_lines(
    text: str,
    keywords: list[str],
    max_lines: int,
    window_start: datetime,
) -> tuple[list[str], list[str]]:
    """按时间窗、模块噪音和关键词筛选日志行。"""
    candidates: list[str] = []
    last_seen_in_window: Optional[bool] = None
    last_seen_was_meta = False
    for line in text.splitlines():
        if not line.strip():
            continue
        timestamp = parse_line_timestamp(line)
        if timestamp is not None:
            in_window = timestamp >= window_start
            meta = is_meta_noise(line)
            last_seen_was_meta = meta
            last_seen_in_window = in_window and not meta
            if in_window and not meta:
                candidates.append(line)
        elif last_seen_in_window and not last_seen_was_meta:
            candidates.append(line)

    if not candidates:
        return [], []
    if not keywords:
        return [], []

    lowered_keywords = [item.lower() for item in keywords]
    matched: list[str] = []
    matched_keywords: set[str] = set()
    keep_block = False
    for line in candidates:
        has_timestamp = parse_line_timestamp(line) is not None
        if has_timestamp:
            line_keywords = [
                keyword for keyword, lowered in zip(keywords, lowered_keywords)
                if lowered in line.lower()
            ]
            keep_block = bool(line_keywords)
            if keep_block:
                matched_keywords.update(line_keywords)
                matched.append(line)
        elif keep_block:
            matched.append(line)
    if matched:
        compacted = _compact_repeated_lines(matched)
        return compacted[-max_lines:], sorted(matched_keywords)
    return [], []


def collect_diagnostics(
    *,
    original_user_request: str,
    keywords: list[str],
    max_lines: int,
    time_window_minutes: int,
) -> dict:
    """读取日志、筛选、脱敏并写入运行时诊断文件。"""
    if not has_explicit_feedback_intent(original_user_request):
        return {
            "success": False,
            "reason": "no_explicit_feedback_intent",
            "message": (
                "用户原话里没有明确要求向上游反馈 Issue 的短语，"
                "请先回到常规诊断路径；只有明确说出反馈 issue / 提 issue / 报 bug "
                "等意图时才运行 feedback-issue 流程。"
            ),
        }

    normalized_max_lines = min(max(int(max_lines or 80), 20), 200)
    window_minutes = normalize_window(time_window_minutes)
    window_start = datetime.now() - timedelta(minutes=window_minutes)
    normalized_keywords = normalize_keywords(keywords)
    collected: list[str] = []
    source_files: list[str] = []
    matched_files: list[dict] = []

    for path in candidate_log_files():
        text = read_tail(path)
        if not text:
            continue
        lines, matched_keywords = filter_lines(
            text=text,
            keywords=normalized_keywords,
            max_lines=normalized_max_lines,
            window_start=window_start,
        )
        if not lines:
            continue
        source_files.append(str(path))
        matched_files.append({
            "path": str(path),
            "matched_keywords": matched_keywords,
            "line_count": len(lines),
        })
        collected.append(f"### {path.name}\n" + "\n".join(lines))

    logs = sanitize_logs("\n\n".join(collected), MAX_LOGS_CHARS)
    log_selection = {
        "strategy": "time_window_and_keyword_block_match",
        "time_window_minutes": window_minutes,
        "window_start": window_start.isoformat(timespec="seconds"),
        "keywords": normalized_keywords,
        "max_lines_per_file": normalized_max_lines,
        "matched_files": matched_files,
        "warning": (
            "未提供具体关键词，已跳过日志正文收集以避免误带无关日志。"
            if not normalized_keywords else ""
        ),
    }
    diagnostics_file = runtime_file("diagnostics", ".json")
    diagnostics = {
        "original_user_request": original_user_request,
        "keywords": normalized_keywords,
        "found": bool(logs.strip()),
        "logs": logs,
        "log_selection": log_selection,
        "doctor": collect_doctor_report(),
        "source_files": source_files,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_file(diagnostics_file, diagnostics)
    return {
        "success": True,
        "found": diagnostics["found"],
        "diagnostics_file": str(diagnostics_file),
        "runtime_dir": str(feedback_runtime_dir()),
        "source_files": source_files,
        "log_selection_summary": format_log_selection(log_selection),
        "log_bytes": len(logs.encode("utf-8", errors="replace")),
        "log_lines": len(logs.splitlines()) if logs else 0,
        "doctor_collected": bool(diagnostics["doctor"].get("success")),
        "message": (
            "已收集并写入反馈诊断日志文件。"
            if logs
            else "已完成诊断日志收集，但未找到明显相关日志。"
        ),
    }


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="收集 MoviePilot 反馈 Issue 诊断日志")
    parser.add_argument("--original-user-request", required=True, help="触发反馈的用户原话")
    parser.add_argument("--keyword", action="append", default=[], help="用于过滤日志的具体关键词，可重复")
    parser.add_argument("--max-lines", type=int, default=80, help="最多保留的日志行数")
    parser.add_argument(
        "--time-window-minutes",
        type=int,
        default=_DEFAULT_TIME_WINDOW_MINUTES,
        help="只收集最近 N 分钟日志，默认 30",
    )
    return parser.parse_args()


def main() -> int:
    """脚本入口：输出 JSON 结果给 Agent 解析。"""
    args = parse_args()
    result = collect_diagnostics(
        original_user_request=args.original_user_request,
        keywords=args.keyword,
        max_lines=args.max_lines,
        time_window_minutes=args.time_window_minutes,
    )
    print(result_payload(**result))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
