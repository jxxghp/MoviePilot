"""收集反馈 Issue 提交前需要附带的本地诊断日志。"""

from __future__ import annotations

import json
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
        description="Short keywords to filter logs, e.g. media title, module name, TMDB, error text",
    )
    max_lines: int = Field(
        default=80,
        description="Maximum matched log lines to return; default 80",
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
        """合并用户原话和显式关键词，生成保守的日志过滤词。"""
        normalized: list[str] = []
        for item in keywords or []:
            item = str(item or "").strip()
            if len(item) >= 2 and item not in normalized:
                normalized.append(item)
        for marker in ("TMDB", "tmdb", "识别", "整理", "失败", "错误", "异常"):
            if marker in original_user_request and marker not in normalized:
                normalized.append(marker)
        return normalized

    @staticmethod
    def _filter_lines(text: str, keywords: list[str], max_lines: int) -> list[str]:
        """按关键词筛选日志行；没有关键词时取尾部行。"""
        lines = [line for line in text.splitlines() if line.strip()]
        if keywords:
            lowered_keywords = [item.lower() for item in keywords]
            matched = [
                line
                for line in lines
                if any(keyword in line.lower() for keyword in lowered_keywords)
            ]
            if matched:
                return matched[-max_lines:]
        return lines[-max_lines:]

    async def run(
        self,
        original_user_request: str,
        keywords: Optional[list[str]] = None,
        max_lines: int = 80,
        **kwargs,
    ) -> str:
        """读取、筛选、脱敏并缓存本次反馈相关日志。"""
        try:
            normalized_max_lines = min(max(int(max_lines or 80), 20), 200)
        except (TypeError, ValueError):
            normalized_max_lines = 80

        normalized_keywords = self._normalize_keywords(original_user_request, keywords)
        collected: list[str] = []
        source_files: list[str] = []

        for path in self._candidate_log_files():
            text = self._read_tail(path)
            if not text:
                continue
            lines = self._filter_lines(text, normalized_keywords, normalized_max_lines)
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
