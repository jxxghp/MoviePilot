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

# 提取日志行的源模块名，用于过滤"Agent 自身 meta-noise"。
_LOG_MODULE_RE = re.compile(
    r"^【[^】]+】\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+\s+-\s+([^\s][^\-]*?)\s+-\s+"
)

# 这些模块产出的日志属于 Agent 自身运行 / 框架内务，对用户面故障定位毫无
# 价值——反而经常把诊断段污染成"反馈流程的回声"：tool args dump 里塞着
# ``database / 推荐 / 豆包`` 等关键字，让 keyword 过滤命中一堆 noise，
# 真正的 RateLimitError / Traceback 反而被挤掉（参见 #5808 实战）。
#
# 包含两类：
# 1) 反馈流程自己的工具与框架（绝对要排除，否则永远在自我反射）
# 2) 通用 Agent 框架噪音：tool dispatch / event bus / streaming callback /
#    通知发送 / activity log 等
_META_NOISE_MODULES = frozenset({
    # 反馈流程
    "collect_feedback_diagnostics.py",
    "prepare_feedback_issue.py",
    "submit_feedback_issue.py",
    "ask_user_choice.py",
    # Agent 框架
    "base.py",          # tool framework: Executing tool / Tool ... executed
    "agent",            # agent runtime: Agent推理 / 流式输出
    "factory.py",       # tool factory creation
    "callback",         # streaming callback
    "prompt",           # 提示词加载
    "memory.py",        # 会话记忆
    "activity_log.py",  # activity 日志
    # 消息/事件总线（往往把 issue 预览全文 dump 进日志）
    "message.py",
    "event.py",
    "chain",            # chain - 请求系统模块执行：xxx
    # 渠道适配层噪音
    "discord",
    "telegram",
    "telegram.py",
    # 命令执行（agent 自己跑过的 shell 命令 echo）
    "execute_command.py",
})

# 不允许使用的模糊关键词：通用到几乎每条 log 都会命中、对定位本次问题
# 没有价值。当 keyword 列表只剩这些时退回到「按时间窗口取尾部」。
_VAGUE_KEYWORDS = frozenset({
    "错误", "异常", "失败", "error", "exception", "failed", "warn", "warning",
    "日志", "问题", "bug", "log", "logs",
})

# 入口意图门：``original_user_request`` 里必须能同时命中"动作"+"目标"，
# 工具才允许进入反馈流程。Agent 在用户随口提到「报错」「不工作」时自作
# 主张调用本工具，就会被这里硬挡住——把反馈通道留给真正想给上游提
# Issue 的请求。
#
# 当前威胁模型是「模型过度归因到 upstream bug」，不是「对抗性绕过」；
# 用户用近义词意图明显时（如「能不能给上游提 issue」），SKILL.md 引导
# Agent 在原话里至少保留 ``反馈/提交/上游/issue`` 之一；如果保留不下来，
# Agent 应该回退到本地诊断而不是强行触发反馈。
#
# 第一组动作词（必须出现至少一个）：
_FEEDBACK_VERB_PHRASES: tuple[str, ...] = (
    "反馈", "提交", "上报", "汇报",
    "提 issue", "提issue", "提 bug", "提bug",
    "报 bug", "报bug", "报告 bug", "报告bug",
    "新建 issue", "新建issue", "开 issue", "开issue",
    "让上游", "给上游",
    "file an issue", "report a bug", "open an upstream issue",
    "submit an issue", "raise an issue", "report this upstream",
    "report upstream",
)
# 第二组目标词（动作命中后再校验目标存在）：英文 phrase 自带目标可绕过这里。
_FEEDBACK_TARGET_TOKENS: tuple[str, ...] = (
    "issue", "bug", "问题", "错误报告",
    "上游", "mp", "moviepilot",
)
# 自带目标语义的完整短语：命中后直接放行，不再校验目标词。
_FEEDBACK_STANDALONE_PHRASES: tuple[str, ...] = (
    "file an issue", "report a bug", "open an upstream issue",
    "submit an issue", "raise an issue", "report this upstream",
    "report upstream",
    "新建 issue", "新建issue", "开 issue", "开issue",
    "提 issue", "提issue", "提 bug", "提bug",
    "报 bug", "报bug", "报告 bug", "报告bug",
    "让上游", "给上游",
)
# 中文里常见"动词 + 量词/介词 + 目标"模式，用正则承接（最多容忍 6 字符
# 间隔，覆盖"给 MP 提个 bug"、"反馈这个问题"、"报告一个 issue"）：
_FEEDBACK_REGEX_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"提.{0,6}(bug|issue|问题|错误报告)", re.IGNORECASE),
    re.compile(r"报.{0,6}(bug|issue|错误报告)", re.IGNORECASE),
    re.compile(r"反馈.{0,8}(issue|bug|问题|上游|错误)", re.IGNORECASE),
    re.compile(r"开.{0,4}(issue|bug)", re.IGNORECASE),
    re.compile(r"上报.{0,6}(bug|issue|问题|错误)", re.IGNORECASE),
)


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
    def _has_explicit_feedback_intent(original_user_request: str) -> bool:
        """判断用户原话里是否出现了"明确要求提 Issue"的意图。

        Why: Agent 在 deepseek 这类强模型里会主动归因——用户只说"TMDB 报
        错"或"下载没动"，Agent 就跳过本地诊断、直接进入反馈流程。本工具
        是反馈流程的入口，硬挡一道意图门，迫使 Agent 回到 SKILL.md Step 0
        要求的"先排查、再反馈"路径。

        判定规则（先放行更具体的、再回落到组合）：
        1. 命中 ``_FEEDBACK_STANDALONE_PHRASES`` 任一短语 → 放行。
           这些短语已经把"动作 + 目标"打包在一起（如 ``提 issue``、
           ``file an issue``），无需再二次校验。
        2. 同时命中一个 ``_FEEDBACK_VERB_PHRASES`` 动作词和一个
           ``_FEEDBACK_TARGET_TOKENS`` 目标词 → 放行。能覆盖"反馈这个
           问题"、"提交个 bug"、"把这个反馈给上游"等自然中文。
        3. 否则视为没有明确意图，拒绝。
        """
        if not original_user_request:
            return False
        normalized = original_user_request.lower().strip()

        if any(phrase in normalized for phrase in _FEEDBACK_STANDALONE_PHRASES):
            return True
        if any(p.search(normalized) for p in _FEEDBACK_REGEX_PATTERNS):
            return True
        has_verb = any(phrase in normalized for phrase in _FEEDBACK_VERB_PHRASES)
        has_target = any(token in normalized for token in _FEEDBACK_TARGET_TOKENS)
        return has_verb and has_target

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

    @staticmethod
    def _is_meta_noise(line: str) -> bool:
        """判断一行日志是否来自"Agent 自身 meta-noise"模块。

        命中即排除。续行（无模块名）由调用方按"跟随父行"语义处理。
        """
        match = _LOG_MODULE_RE.match(line)
        if not match:
            return False
        return match.group(1).strip() in _META_NOISE_MODULES

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
        last_seen_was_meta: bool = False
        for line in text.splitlines():
            if not line.strip():
                continue
            ts = cls._parse_line_timestamp(line)
            if ts is not None:
                in_window = ts >= window_start
                # Meta-noise 行（agent/tool framework 自己的日志）即便落在窗口
                # 内也直接丢；它们对用户面故障定位没有价值，反而会因为带有
                # ``database / 推荐 / 豆包`` 之类关键字让诊断段灌满 noise。
                is_meta = cls._is_meta_noise(line)
                last_seen_was_meta = is_meta
                last_seen_in_window = in_window and not is_meta
                if in_window and not is_meta:
                    candidates.append(line)
            else:
                # 续行：跟随上一条时间戳行的去留（meta-noise 父行的续行也丢）
                if last_seen_in_window and not last_seen_was_meta:
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

        反馈入口意图门（用户反馈）：``original_user_request`` 里必须有
        明确"我要提 Issue / 反馈 issue / file an issue"之类的短语；
        Agent 自作主张把"TMDB 报错"理解成"反馈" 时直接拒绝，引导回归
        本地诊断路径，避免给上游刷 Issue。
        """
        if not self._has_explicit_feedback_intent(original_user_request):
            logger.info(
                "collect_feedback_diagnostics 拒绝：原始请求里没有明确"
                "反馈意图。原话=%r",
                (original_user_request or "")[:120],
            )
            return json.dumps(
                {
                    "success": False,
                    "reason": "no_explicit_feedback_intent",
                    "message": (
                        "用户原话里没有明确要求向上游反馈 Issue 的短语，"
                        "不应直接进入反馈流程。请回到常规诊断路径，使用"
                        "query_subscribes / query_download_tasks / "
                        "query_logs / test_site 等工具先排查；仅当用户"
                        "在排查后明确要求把问题转给上游（例如说出 "
                        "「反馈 issue / 提 issue / 报 bug / 让上游修一下」"
                        "之类的原话），才能再次调用本工具。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )

        try:
            normalized_max_lines = min(max(int(max_lines or 80), 20), 200)
        except (TypeError, ValueError):
            normalized_max_lines = 80

        window_minutes = self._normalize_window(time_window_minutes)
        window_start = datetime.now() - timedelta(minutes=window_minutes)
        normalized_keywords = self._normalize_keywords(original_user_request, keywords)
        collected: list[str] = []
        source_files: list[str] = []

        log_files = await self.run_blocking("default", self._candidate_log_files)
        for path in log_files:
            text = await self.run_blocking("default", self._read_tail, path)
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
