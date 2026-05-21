"""收集反馈 Issue 提交前需要附带的本地诊断日志。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.feedback_issue_state import feedback_issue_state_store
from app.agent.tools.impl.submit_feedback_issue import SubmitFeedbackIssueTool
from app.core.config import settings
from app.log import logger


_MAX_READ_BYTES = 512 * 1024
_MAX_DIAGNOSTIC_LOG_CHARS = 6 * 1024

# 默认时间窗：仅收集最近 30 分钟的日志。
# Why: 用户说「今天 TMDB 一直在报错」时，期望看到的是这次会话前后真实
# 触发的报错，而不是几天前历史日志里所有出现 "TMDB" 的行。Issue #5806
# 实战中就发生了：关键词命中了几天前的测试日志，日志段完全对不上当前问题。
_DEFAULT_TIME_WINDOW_MINUTES = 30
_MIN_TIME_WINDOW_MINUTES = 5
_MAX_TIME_WINDOW_MINUTES = 24 * 60

# MoviePilot 主日志行首格式：``【LEVEL】YYYY-MM-DD HH:MM:SS,ms - module - msg``
# 用第一个时间戳判断行属于哪一刻；匹配不到时把行算到「无法判断时间」桶，
# 默认保留（行内可能是 Traceback 续行，不能丢）。
_LOG_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})")
_LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# 不允许使用的模糊关键词：通用到几乎每条 log 都会命中、对定位本次问题
# 没有价值。当 keyword 列表只剩这些时退回到「按时间窗口取尾部」。
_VAGUE_KEYWORDS = frozenset({
    "错误", "异常", "失败", "error", "exception", "failed", "warn", "warning",
    "日志", "问题", "bug", "log", "logs",
})


class CollectFeedbackDiagnosticsInput(BaseModel):
    """反馈诊断日志收集工具输入。"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why diagnostic logs are being collected before filing feedback",
    )
    original_user_request: str = Field(
        ...,
        description="The user's original bug report text that triggered diagnostics collection",
    )
    keywords: Optional[list[str]] = Field(
        default=None,
        description=(
            "Short keywords to filter logs. Should be SPECIFIC tokens: media title, "
            "plugin id, exception class name, downloader name, etc. Vague terms like "
            "'错误'/'异常'/'失败'/'error' are ignored because they match almost every log line."
        ),
    )
    max_lines: int = Field(
        default=80,
        description="Maximum matched log lines to return; default 80",
    )
    time_window_minutes: int = Field(
        default=_DEFAULT_TIME_WINDOW_MINUTES,
        description=(
            "Only include log lines whose timestamp falls within the last N minutes "
            "(default 30, range 5-1440). Older lines are dropped regardless of keyword "
            "match so the diagnostic snapshot reflects the current incident, not "
            "historical noise."
        ),
    )


class CollectFeedbackDiagnosticsTool(MoviePilotTool):
    """收集并缓存反馈 Issue 用的日志片段。"""

    name: str = "collect_feedback_diagnostics"
    description: str = (
        "Collect recent local MoviePilot logs before preparing or submitting a feedback issue. "
        "This tool reads config/logs/moviepilot.log and plugin logs, filters by user-provided "
        "keywords when available, redacts common secrets, and stores a diagnostics_id that "
        "submit_feedback_issue requires. Use it before prepare_feedback_issue."
    )
    args_schema: Type[BaseModel] = CollectFeedbackDiagnosticsInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """侧边消息：告知用户正在读取本地日志辅助反馈。"""
        return "收集反馈诊断日志"

    @staticmethod
    def _read_tail(path: Path) -> str:
        """读取日志文件尾部，避免大日志一次性进入内存。"""
        try:
            size = path.stat().st_size
            with path.open("rb") as file_obj:
                if size > _MAX_READ_BYTES:
                    file_obj.seek(size - _MAX_READ_BYTES)
                return file_obj.read().decode("utf-8", errors="replace")
        except OSError as err:
            logger.debug("读取反馈诊断日志失败: %s %s", path, err)
            return ""

    @staticmethod
    def _candidate_log_files() -> list[Path]:
        """返回反馈诊断可读取的日志文件列表。"""
        files = [settings.LOG_PATH / "moviepilot.log"]
        plugin_log_dir = settings.LOG_PATH / "plugins"
        if plugin_log_dir.exists():
            files.extend(sorted(plugin_log_dir.rglob("*.log")))
        return [path for path in files if path.exists() and path.is_file()]

    @staticmethod
    def _normalize_keywords(
        original_user_request: str,
        keywords: Optional[list[str]],
    ) -> list[str]:
        """合并用户原话和显式关键词，生成保守的日志过滤词。

        Issue #5806 教训：把 "错误 / 异常 / 失败 / TMDB" 这种通用词当关键词
        会让几乎所有日志行命中，过滤等于没过滤。这里只保留**显式且足够具体**
        （≥2 字符且不在 ``_VAGUE_KEYWORDS`` 里）的关键词。"""
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

    @staticmethod
    def _normalize_window(time_window_minutes: int) -> int:
        """把传入的时间窗 clamp 到 [5, 1440] 区间。"""
        try:
            window = int(time_window_minutes or _DEFAULT_TIME_WINDOW_MINUTES)
        except (TypeError, ValueError):
            window = _DEFAULT_TIME_WINDOW_MINUTES
        return max(_MIN_TIME_WINDOW_MINUTES, min(_MAX_TIME_WINDOW_MINUTES, window))

    @staticmethod
    def _parse_line_timestamp(line: str) -> Optional[datetime]:
        """从一行日志开头提取时间戳；提取不到返回 None。"""
        match = _LOG_TIMESTAMP_RE.search(line[:64])
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), _LOG_TIMESTAMP_FORMAT)
        except ValueError:
            return None

    @classmethod
    def _filter_lines(
        cls,
        text: str,
        keywords: list[str],
        max_lines: int,
        window_start: datetime,
    ) -> list[str]:
        """按时间窗 + 关键词筛日志。

        - 行能解析到时间戳：在 ``window_start`` 之前的丢弃；之后的进入候选。
        - 行解析不到时间戳（Traceback 续行等）：跟随**最近一条已知时间戳行**
          的归属，没有上下文时按"近期"对待，避免把异常堆栈截断。
        - 在候选行里再按关键词过滤；无关键词或全部行都不命中时退回到时间
          窗内的尾部行，保证返回有意义的内容而不是空集。
        """
        candidates: list[str] = []
        last_seen_in_window: Optional[bool] = None
        for line in text.splitlines():
            if not line.strip():
                continue
            ts = cls._parse_line_timestamp(line)
            if ts is not None:
                in_window = ts >= window_start
                last_seen_in_window = in_window
                if in_window:
                    candidates.append(line)
            else:
                # 续行：跟随上一条时间戳的窗口判断；起始连续无时间戳行直接丢
                if last_seen_in_window:
                    candidates.append(line)

        if not candidates:
            return []
        if keywords:
            lowered_keywords = [item.lower() for item in keywords]
            # 关键字过滤需要按"时间戳行块"为单位：命中的 ERROR 行带着它的
            # Traceback 续行一起保留，避免把异常堆栈截掉一半反而更难定位。
            matched: list[str] = []
            keep_block = False
            for line in candidates:
                has_ts = cls._parse_line_timestamp(line) is not None
                if has_ts:
                    keep_block = any(kw in line.lower() for kw in lowered_keywords)
                    if keep_block:
                        matched.append(line)
                elif keep_block:
                    matched.append(line)
            if matched:
                return matched[-max_lines:]
        return candidates[-max_lines:]

    async def run(
        self,
        original_user_request: str,
        keywords: Optional[list[str]] = None,
        max_lines: int = 80,
        time_window_minutes: int = _DEFAULT_TIME_WINDOW_MINUTES,
        **kwargs,
    ) -> str:
        """读取、筛选、脱敏并缓存本次反馈相关日志。

        Issue #5806 暴露的两个数据准确性问题在这里一并修：
        1. 时间窗：默认只看最近 30 分钟，杜绝历史无关日志混入。
        2. 关键词过滤收紧：剔除"错误/异常/失败"等几乎每行都命中的通用词。
        """
        try:
            normalized_max_lines = min(max(int(max_lines or 80), 20), 200)
        except (TypeError, ValueError):
            normalized_max_lines = 80

        window_minutes = self._normalize_window(time_window_minutes)
        window_start = datetime.now() - timedelta(minutes=window_minutes)
        normalized_keywords = self._normalize_keywords(original_user_request, keywords)
        collected: list[str] = []
        source_files: list[str] = []

        for path in self._candidate_log_files():
            text = self._read_tail(path)
            if not text:
                continue
            lines = self._filter_lines(
                text, normalized_keywords, normalized_max_lines, window_start
            )
            if not lines:
                continue
            source_files.append(str(path))
            collected.append(f"### {path.name}\n" + "\n".join(lines))

        raw_logs = "\n\n".join(collected)
        logs = SubmitFeedbackIssueTool._sanitize_logs(raw_logs, _MAX_DIAGNOSTIC_LOG_CHARS)
        found = bool(logs.strip())

        record = feedback_issue_state_store.create_diagnostics(
            session_id=self._session_id,
            user_id=self._user_id,
            username=self._username,
            logs=logs,
            source_files=source_files,
            found=found,
        )
        self._agent_context["feedback_issue_diagnostics_id"] = record.diagnostics_id

        # 关键：不要把 ``logs`` 内容回传给 LLM。日志可达 6KB，回传后 LLM
        # 还会在下一步把它原样塞进 prepare_feedback_issue 的入参里二次
        # transit，导致 26B/V3 等模型每轮要 ingest+emit 数 KB 文本，响应延
        # 迟从秒级飙到分钟级（曾观察到 collect 返回 7.7KB → 下一轮 prepare
        # 入参 logs 字段又重复一份）。日志全程只通过 ``diagnostics_id``
        # 在服务端的 ``feedback_issue_state_store`` 流转，模型只看到摘要。
        log_bytes = len(record.logs.encode("utf-8", errors="replace"))
        log_lines = len(record.logs.splitlines()) if record.logs else 0
        return json.dumps(
            {
                "success": True,
                "diagnostics_id": record.diagnostics_id,
                "found": record.found,
                "source_files": record.source_files,
                "log_bytes": log_bytes,
                "log_lines": log_lines,
                "message": (
                    "已收集并缓存反馈诊断日志。"
                    if found
                    else "已完成诊断日志收集，但未找到明显相关日志。"
                ) + (
                    "日志已通过 diagnostics_id 缓存在服务端，"
                    "后续 prepare_feedback_issue / submit_feedback_issue "
                    "只需传入 diagnostics_id，**不要**再把日志正文当参数传回。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
