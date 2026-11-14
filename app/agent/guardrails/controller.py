"""
检测单轮工具调用中的重复失败与无进展循环。

版权所有 (c) 2025 Nous Research；授权条款见随附的 MIT 许可证。

控制器只维护本轮观测状态并返回决策，不直接执行工具或发出消息。
运行时负责把决策转换为警告、合成工具结果或受控的轮次停止。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

from app.agent.guardrails.results import file_mutation_result_landed, is_guardrail_refusal, safe_json_loads

IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "session_search", "skill_view", "skills_list",
    "browser_snapshot", "browser_console", "browser_get_images", "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file", "mcp_filesystem_read_multiple_files", "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes", "mcp_filesystem_directory_tree", "mcp_filesystem_get_file_info",
    "mcp_filesystem_search_files",
})

MUTATING_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "write_file", "patch", "todo_list", "memory", "skill_manage",
    "browser_click", "browser_type", "browser_press", "browser_scroll", "browser_navigate",
    "send_message", "cronjob_manage", "delegate_task", "process_manage",
})

# 轮询工具允许使用相同参数再次查询，不产生相同调用循环提示。
STALL_GUARD_REPEATABLE_TOOLS = frozenset({"process_manage"})
# 自动生成工具和 MCP 轮询工具约定的名称后缀。
_STALL_GUARD_REPEATABLE_SUFFIXES = ("_get_result", "_poll")
# 连续相同调用达到 3 次才提示，允许一次重复确认。
STALL_GUARD_IDENTICAL_CALL_THRESHOLD = 3
# 多步骤循环会在每次交替时打断连续重复计数，因此另外检测最长 4 步的周期。
# 周期提示和停止复用连续重复的圈数阈值；实现参考 can1357/oh-my-pi#10521。
_STALL_GUARD_MAX_CYCLE_PERIOD = 4
# 保留 64 次调用，为最长周期达到停止阈值留出足够历史空间。
_STALL_GUARD_CYCLE_HISTORY = 64
# 第二次重复的大结果改用引用；小结果不值得替换，失败结果始终保留。
# 参数预览使原始上下文被压缩后仍可辨认调用内容。
IDENTICAL_RESULT_STUB_MIN_CHARS = 512
_RESULT_STUB_ARGS_PREVIEW_CHARS = 120

# 测试失败、搜索无匹配或网页超时可能是正常工作结果。
# 这些工具使用不同参数连续失败时只警告；无变更的相同参数重放仍受限制。
FAILURE_TOLERANT_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "process_manage", "process", "browser_navigate", "web_extract",
})

# 这些工具成功后为本轮仍计数的失败调用标记进展，使修改后的重试可以重新计数。
PROGRESS_RESET_TOOL_NAMES = frozenset({
    "write_file", "patch", "terminal", "execute_code", "browser_click", "browser_type", "browser_press",
    "browser_navigate", "process_manage", "process", "delegate_task", "send_message", "cronjob",
    "cronjob_manage", "todo", "todo_list", "memory", "skill_manage",
})

_BOOL_FIELDS = ("warnings_enabled", "hard_stop_enabled", "non_interactive_hard_stop_enabled")
# 阈值字段映射到嵌套配置节和键；旧版平铺配置直接使用字段名。
_THRESHOLD_SOURCES: dict[str, tuple[str, str]] = {
    "exact_failure_warn_after": ("warn_after", "exact_failure"),
    "same_tool_failure_warn_after": ("warn_after", "same_tool_failure"),
    "no_progress_warn_after": ("warn_after", "idempotent_no_progress"),
    "exact_failure_block_after": ("hard_stop_after", "exact_failure"),
    "same_tool_failure_halt_after": ("hard_stop_after", "same_tool_failure"),
    "no_progress_block_after": ("hard_stop_after", "idempotent_no_progress"),
}

# 容易失控的工具具有单轮硬上限，计数由 reset_for_turn 重置。
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50

# 有人监督的交互入口和有上级控制的子智能体默认只警告，允许修改后重新验证。
_ATTENDED_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp", "subagent", "api_server"})


def is_stall_guard_repeatable(tool_name: str) -> bool:
    """
    判断工具是否属于允许重复查询的轮询工具。

    :return: 为 True 时豁免完全相同调用的循环提示，不影响结果去重
    """
    return tool_name in STALL_GUARD_REPEATABLE_TOOLS or tool_name.endswith(_STALL_GUARD_REPEATABLE_SUFFIXES)


def _is_non_interactive_platform(platform: str | None) -> bool:
    """
    判断工具循环是否运行在无人值守的网关或定时任务入口。
    """
    if not isinstance(platform, str) or not platform.strip():
        return False
    return platform.strip().lower() not in _ATTENDED_PLATFORMS


@dataclass(frozen=True)
class LoopCapConfig:
    """
    单轮网络搜索次数和子智能体创建数量的硬上限。

    统计累计调用或创建数量，不受 hard_stop_enabled 开关影响；0 表示不限制。

    :param max_web_searches: 每轮 web_search 调用上限，默认 50；0 表示不限制
    :param max_subagents: 每轮创建子智能体的数量上限，默认 50；0 表示不限制
    """

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """
        从 tool_loop_guardrails.loop_caps 配置读取本轮硬上限。

        :param data: 配置映射；不是映射时使用默认值，非法或负数上限回退到对应默认值
        :return: 规范化后的调用上限配置
        """
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(**{f.name: _int_at_least(data.get(f.name), getattr(defaults, f.name), 0) for f in fields(cls)})


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """
    单轮工具循环检测的阈值及启停策略。

    警告不阻止执行。交互入口默认只警告，无人值守入口默认启用硬停止。

    :param warnings_enabled: 是否向模型提供循环警告，默认启用
    :param hard_stop_enabled: 是否在检测到循环时硬停止，默认关闭
    :param non_interactive_hard_stop_enabled: 是否对无人值守入口自动启用硬停止，默认启用
    :param exact_failure_warn_after: 相同参数调用连续失败后开始警告的次数，默认 2
    :param exact_failure_block_after: 相同参数调用持续失败后禁止再执行的次数，默认 5
    :param same_tool_failure_warn_after: 同一工具累计失败后开始警告的次数，默认 3
    :param same_tool_failure_halt_after: 同一工具累计失败后停止本轮的次数，默认 8；容错工具仅警告
    :param no_progress_warn_after: 幂等读取返回相同结果后开始警告的次数，默认 2
    :param no_progress_block_after: 无进展读取或相同调用达到硬停止的次数，默认 5
    :param idempotent_tools: 可以参与重复读取检测的已知幂等工具集合
    :param mutating_tools: 具有变更能力的工具集合，排除在幂等读取检测之外
    :param progress_tools: 成功后视为取得进展、允许失败调用重新尝试的工具集合
    :param loop_caps: 独立于循环硬停止开关的单轮调用数量限制
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    non_interactive_hard_stop_enabled: bool = True
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    progress_tools: frozenset[str] = field(default_factory=lambda: PROGRESS_RESET_TOOL_NAMES)
    loop_caps: LoopCapConfig = field(default_factory=LoopCapConfig)

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any] | None, *, platform: str | None = None,
    ) -> "ToolCallGuardrailConfig":
        """
        从 tool_loop_guardrails 配置读取阈值与入口策略。

        嵌套的 warn_after 和 hard_stop_after 优先于旧版平铺字段。

        :param data: 原始配置；不是映射时按空配置处理
        :param platform: 调用入口；无人值守入口可按配置启用硬停止
        :return: 解析后的阈值、工具集合及调用上限
        """
        if not isinstance(data, Mapping):
            data = {}
        d = cls()
        flags: dict[str, Any] = {name: _as_bool(data.get(name), getattr(d, name)) for name in _BOOL_FIELDS}
        if flags["non_interactive_hard_stop_enabled"] and _is_non_interactive_platform(platform):
            flags["hard_stop_enabled"] = True

        def threshold(name: str, section_name: str, key: str) -> int:
            """
            优先解析嵌套阈值，再兼容旧版平铺字段。
            """
            section = data.get(section_name)
            nested = section.get(key, data.get(name)) if isinstance(section, Mapping) else data.get(name)
            return _int_at_least(nested, getattr(d, name), 1)

        thresholds: dict[str, Any] = {name: threshold(name, *src) for name, src in _THRESHOLD_SOURCES.items()}
        return cls(loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")), **flags, **thresholds)


@dataclass(frozen=True)
class IdenticalCallObservation:
    """
    同一次调用的循环提示与结果去重信息。

    :param notice: 追加到结果后的循环提示；未触发时为 None
    :param stub: 替代重复大结果的引用文本；无需去重时为 None
    :param kind: 触发提示的检测器：identical_call_streak 或 identical_cycle；未触发时为 None
    """

    notice: str | None = None
    stub: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class ToolCallSignature:
    """
    工具名称与规范化参数构成的稳定、不可逆调用标识。

    :param tool_name: 调用的工具名称
    :param args_hash: 规范化参数的 SHA-256 摘要，不保存原始参数值
    """

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        """
        对规范化参数取哈希，避免诊断元数据暴露凭据。

        :param tool_name: 实际调用的工具名称
        :param args: 工具参数映射；None 视为空参数
        :return: 不含原始参数值的调用标识
        """
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args or {})))

    def to_metadata(self) -> dict[str, str]:
        """
        返回不包含原始参数值的公共调用元数据。
        """
        return asdict(self)


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """
    工具循环控制器返回的执行决定。

    :param action: 执行动作：allow 放行、warn 警告、block 阻止或 halt 停止
    :param code: 供宿主和诊断识别的稳定决策原因码
    :param message: 面向模型的操作建议；无需提示时为空字符串
    :param tool_name: 触发当前决策的工具名称
    :param count: 触发当前决策的调用次数或观测次数
    :param signature: 关联调用的不可逆标识；没有关联时为 None
    """

    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        """
        判断本次调用是否可以继续执行。

        :return: allow 和 warn 允许执行，block 和 halt 禁止执行
        """
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        """
        判断当前轮次是否需要受控停止。

        :return: block 和 halt 均要求停止无人值守轮次
        """
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        """
        返回决策元数据，不包含原始工具参数；无调用标识时省略对应字段。
        """
        data = asdict(self)
        if data["signature"] is None:
            del data["signature"]
        return data


def _canonical_json(value: Any) -> str:
    """
    以稳定键顺序和紧凑 JSON 形式序列化嵌套工具参数。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """
    将工具参数规范化为排序后的紧凑 JSON。

    :param args: 已解析的工具参数映射
    :return: 用于计算稳定调用标识的 JSON 文本
    :raises TypeError: 参数不是映射
    """
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return _canonical_json(args)


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """
    在调用方未明确提供 failed 时，根据工具回执判断是否失败。

    已确认落盘的写操作及宿主主动拒绝重复调用的回执不视为执行失败。

    :param tool_name: 产生回执的工具名称
    :param result: 工具文本结果；None 表示没有可判断的失败结果
    :return: 是否失败及供诊断使用的结果摘要
    """
    if result is None or file_mutation_result_landed(tool_name, result):
        return False, ""

    # 宿主主动拒绝重复调用的回执也可能包含 error，但并非真实工具失败。
    # 若把拒绝继续计入失败，会使一次轻量拒绝触发更严厉的循环限制。
    if is_guardrail_refusal(result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        return (True, f" [exit {exit_code}]") if exit_code is not None and exit_code != 0 else (False, "")

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict) and data.get("success") is False and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
    lower = result[:500].lower()
    return (True, " [error]") if '"error"' in lower or '"failed"' in lower or result.startswith("Error") else (False, "")


# 按原因码选择注入会话的指导文本；同工具失败提示由专用函数按工具类型生成。
_DECISION_MESSAGES: dict[str, str] = {
    "repeated_exact_failure_block": (
        "Blocked {tool_name}: the same tool call failed {count} times with identical arguments. "
        "Stop retrying it unchanged; change strategy or explain the blocker."
    ),
    "idempotent_no_progress_block": (
        "Blocked {tool_name}: this read-only call returned the same result {count} times. "
        "Stop repeating it unchanged; use the result already provided or try a different query."
    ),
    "same_tool_failure_halt": (
        "Stopped {tool_name}: it failed {count} times this turn. "
        "Stop retrying the same failing tool path and choose a different approach."
    ),
    "repeated_exact_failure_warning": (
        "{tool_name} has failed {count} times with identical arguments. This looks like a loop; "
        "inspect the error and change strategy instead of retrying it unchanged."
    ),
    "idempotent_no_progress_warning": (
        "{tool_name} returned the same result {count} times. Use the result already provided "
        "or change the query instead of repeating it unchanged."
    ),
    "identical_call_streak_halt": (
        "Stopped {tool_name}: the same call with identical arguments returned the same result "
        "{count} times in a row. Stop repeating it unchanged; use the result already provided or change strategy."
    ),
    "identical_cycle_halt": (
        "Stopped {tool_name}: the same repeating cycle of tool calls (period {period}) with identical "
        "arguments and identical results has run {count} times. Repeating the batch unchanged is not "
        "progress; use the results already provided or change strategy."
    ),
    "loop_web_search_cap": (
        "Blocked web_search: this turn has already made {cap} web searches, the per-turn limit. "
        "This looks like a runaway search loop. Work with the results you already have and give the user your answer."
    ),
    "loop_subagent_cap": (
        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "
        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."
    ),
}

_IDENTICAL_CALL_NOTICE = (
    "[hermes note: this is the {ordinal} consecutive identical call to "
    "{tool_name} with identical arguments returning the same result. "
    "Do not repeat it — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

_IDENTICAL_CYCLE_NOTICE = (
    "[hermes note: the last {count} rounds repeated the same cycle of {period} tool calls "
    "(ending with {tool_name}) with identical arguments and identical results. "
    "Do not repeat the batch — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

# 工具名称映射到上限配置字段、控制器计数属性和决策原因码。
_LOOP_CAPS: dict[str, tuple[str, str, str]] = {
    "web_search": ("max_web_searches", "_turn_web_search_count", "loop_web_search_cap"),
    "delegate_task": ("max_subagents", "_turn_subagent_count", "loop_subagent_cap"),
}


class ToolCallGuardrailController:
    """
    维护单轮工具调用的失败计数、重复读取与循环检测状态。
    """

    def __init__(self, config: ToolCallGuardrailConfig | None = None):
        """
        绑定阈值并初始化本轮观测状态；未传配置时使用默认策略。
        """
        self.config = config or ToolCallGuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        """
        清除上一轮的调用标识、计数、结果引用和停止决策。
        """
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        # 按调用标识记录自上次失败后是否出现了成功变更。
        self._progress_since_failure: dict[ToolCallSignature, bool] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # 仅跟踪本轮连续相同的工具、参数及结果；任何差异都会重置计数。
        # 修改后的重新读取及结果变化的轮询不会被误判为连续重复。
        self._identical_streak_sig: ToolCallSignature | None = None
        self._identical_streak_result_hash: str = ""
        self._identical_streak_count: int = 0
        self._identical_streak_first_call_id: str = ""
        # 保留本轮各次调用的标识、结果哈希及轮询豁免状态，以检测交替重复的多步骤循环。
        self._call_history: deque[tuple[ToolCallSignature, str, bool]] = deque(maxlen=_STALL_GUARD_CYCLE_HISTORY)
        # 记录完整结果的落盘路径，避免引用已持久化输出时丢失访问入口。
        self._persisted_result_paths: dict[str, str] = {}
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        """
        返回需要受控停止的检测决策；尚未触发时为 None。
        """
        return self._halt_decision

    @property
    def first_result_call_id(self) -> str:
        """
        返回首次完整结果的调用标识，供宿主判断该结果是否已被上下文压缩移除。
        """
        return self._identical_streak_first_call_id

    def replace_result_reference(self, tool_call_id: str) -> None:
        """
        原始结果离开活跃上下文后，以新完整结果的调用标识替换引用目标。
        """
        self._identical_streak_first_call_id = tool_call_id

    def _decide(
        self, action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature,
        *, message: str | None = None, **fmt: Any,
    ) -> ToolGuardrailDecision:
        """
        构造警告、阻止或停止决策，并把需要停止的决策记录到当前轮次。
        """
        if message is None:
            message = _DECISION_MESSAGES[code].format(tool_name=tool_name, count=count, **fmt)
        decision = ToolGuardrailDecision(action, code, message, tool_name, count, signature)
        if decision.should_halt:
            self._halt_decision = decision
        return decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        """
        执行前检查累计调用上限及已观测的失败或读取重放阈值。

        :param tool_name: 即将执行的工具名称
        :param args: 本次调用参数；None 按空参数处理
        :return: 是否允许调用的决策；达到硬上限时在执行工具之前阻止
        """
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        # 调用数量硬上限独立于 hard_stop_enabled，后者只控制循环检测的硬停止。
        cap_block = self._check_loop_cap(tool_name, args, signature)
        if cap_block is not None or not self.config.hard_stop_enabled:
            return cap_block or allow
        # 上次失败后已有成功变更时，本次重试重新计数。
        exact_count = 0 if self._progress_since_failure.get(signature) else self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            return self._decide("block", "repeated_exact_failure_block", tool_name, exact_count, signature)
        record = self._no_progress.get(signature) if self._is_idempotent(tool_name) else None
        if record is not None and record[1] >= self.config.no_progress_block_after:
            return self._decide("block", "idempotent_no_progress_block", tool_name, record[1], signature)
        return allow

    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        """
        记录失败和未变化的读取结果；成功变更允许后续尝试重新计数。

        :param tool_name: 已执行的工具名称
        :param args: 本次调用参数；None 按空参数处理
        :param result: 工具执行结果文本；没有文本时为 None
        :param failed: 调用方确认的失败状态；None 时根据回执推断
        :return: 更新观测状态后的允许、警告或停止决策
        """
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)
        warnings = self.config.warnings_enabled

        if failed:
            # 相同失败调用之间出现成功变更时，重置相同参数的失败计数。
            if self._progress_since_failure.pop(signature, False):
                self._exact_failure_counts.pop(signature, None)
            exact_count = self._exact_failure_counts[signature] = self._exact_failure_counts.get(signature, 0) + 1
            same_count = self._same_tool_failure_counts[tool_name] = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._no_progress.pop(signature, None)
            # 同工具计数包括不同参数的失败；容错工具可能正在排查问题，因此只警告而不停轮。
            if (
                # 幂等读取之外的成功调用也可能反复返回相同结果。
                # 启用硬停止时，连续相同调用使用同一无进展阈值；轮询工具仍豁免。
                self.config.hard_stop_enabled
                and tool_name not in FAILURE_TOLERANT_TOOL_NAMES
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                return self._decide("halt", "same_tool_failure_halt", tool_name, same_count, signature)
            if warnings and exact_count >= self.config.exact_failure_warn_after:
                return self._decide("warn", "repeated_exact_failure_warning", tool_name, exact_count, signature)
            if warnings and same_count >= self.config.same_tool_failure_warn_after:
                return self._decide(
                    "warn", "same_tool_failure_warning", tool_name, same_count, signature,
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                )
            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        # 成功变更为本轮所有仍在计数的失败标识记录进展；纯重放循环不会获得该重置机会。
        if tool_name in self.config.progress_tools or file_mutation_result_landed(tool_name, result):
            self._progress_since_failure.update(dict.fromkeys(self._exact_failure_counts, True))
            self._same_tool_failure_counts.clear()
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result, tool_name)
        previous = self._no_progress.get(signature)
        repeat_count = previous[1] + 1 if previous is not None and previous[0] == result_hash else 1
        self._no_progress[signature] = (result_hash, repeat_count)
        if warnings and repeat_count >= self.config.no_progress_warn_after:
            return self._decide("warn", "idempotent_no_progress_warning", tool_name, repeat_count, signature)
        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        """
        仅对明确登记为不产生变更的工具执行读取重放检测。
        """
        return tool_name not in self.config.mutating_tools and tool_name in self.config.idempotent_tools

    def observe_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, tool_call_id: str = "", failed: bool = False,
    ) -> IdenticalCallObservation:
        """
        观测连续相同调用并生成循环提示或结果引用。

        同一工具、参数和结果连续出现达到阈值时产生提示，轮询工具豁免提示。
        从第二次相同的大结果起可用引用代替本次结果；工具仍已执行，轮询也参与结果去重。

        :param tool_name: 已执行的工具名称
        :param args: 本次调用的参数映射
        :param result: 工具结果；非字符串结果不参与连续重复检测
        :param tool_call_id: 本次调用标识，用于引用完整结果
        :param failed: 是否为失败结果；失败结果不替换为去重引用
        :return: 循环提示、结果引用和触发检测器的标识；未触发项为 None
        """
        is_plain_str = isinstance(result, str)
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        result_hash = _result_hash(result, tool_name) if is_plain_str else ""

        if is_plain_str and (signature, result_hash) == (self._identical_streak_sig, self._identical_streak_result_hash):
            self._identical_streak_count += 1
        else:
            # 非字符串的多模态结果不参与连续相同结果检测。
            self._identical_streak_sig = signature if is_plain_str else None
            self._identical_streak_result_hash = result_hash
            self._identical_streak_count = 1 if is_plain_str else 0
            self._identical_streak_first_call_id = tool_call_id or ""
        count = self._identical_streak_count

        notice = kind = None
        if not is_stall_guard_repeatable(tool_name) and count >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
            notice, kind = _IDENTICAL_CALL_NOTICE.format(ordinal=_ordinal(count), tool_name=tool_name), "identical_call_streak"
            # 连续重复检测不限于幂等工具，硬停止可防止成功的终端调用无限重放。
            if self.config.hard_stop_enabled and count >= self.config.no_progress_block_after and self._halt_decision is None:
                self._decide("halt", "identical_call_streak_halt", tool_name, count, signature)

        # 多步骤循环每次交替都会重置单次连续计数，因此还需检查整个调用历史的周期。
        if is_plain_str:
            self._call_history.append((signature, result_hash, is_stall_guard_repeatable(tool_name)))
        else:
            self._call_history.clear()
        if notice is None and is_plain_str:
            cycle = self._detect_identical_cycle()
            if cycle is not None:
                period, laps = cycle
                notice, kind = _IDENTICAL_CYCLE_NOTICE.format(count=laps, period=period, tool_name=tool_name), "identical_cycle"
                if self.config.hard_stop_enabled and laps >= self.config.no_progress_block_after and self._halt_decision is None:
                    self._decide("halt", "identical_cycle_halt", tool_name, laps, signature, period=period)

        stub = None
        if isinstance(result, str) and count >= 2 and not failed and len(result) >= IDENTICAL_RESULT_STUB_MIN_CHARS:
            stub = self._build_result_reference_stub(tool_name, args)
        return IdenticalCallObservation(notice=notice, stub=stub, kind=kind)

    def _detect_identical_cycle(self) -> tuple[int, int] | None:
        """
        检测截至最新调用的重复多步骤循环。

        只比较各位置的调用标识与结果哈希均相同的完整周期。
        单次调用循环由连续重复检测负责；全部由轮询工具组成的周期豁免。

        :return: 达到提示次数的最短周期及重复圈数；不存在时为 None
        """
        history = self._call_history
        for period in range(2, _STALL_GUARD_MAX_CYCLE_PERIOD + 1):
            if len(history) < period * STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                continue
            laps = 1
            # 从末尾倒数，统计与最后一圈完全相同的连续圈数。
            while True:
                base = len(history) - period * (laps + 1)
                if base < 0:
                    break
                lap_equal = all(
                    history[base + i][:2] == history[len(history) - period + i][:2]
                    for i in range(period)
                )
                if not lap_equal:
                    break
                laps += 1
            if laps >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                tail = [history[len(history) - period + i] for i in range(period)]
                if all(repeatable for _, _, repeatable in tail):
                    continue
                # 更短的固定子周期已经在前面的周期检查中处理。
                return period, laps
        return None

    def record_persisted_result(self, tool_call_id: str, file_path: str) -> None:
        """
        登记工具结果落盘路径，使后续去重引用仍能找到完整内容。
        """
        if tool_call_id and file_path:
            self._persisted_result_paths[tool_call_id] = file_path

    def _build_result_reference_stub(self, tool_name: str, args: Mapping[str, Any] | None) -> str:
        """
        为完全相同的结果构造包含工具、参数预览及可用落盘路径的引用。
        """
        args_preview = canonical_tool_args(_coerce_args(args))
        if len(args_preview) > _RESULT_STUB_ARGS_PREVIEW_CHARS:
            args_preview = args_preview[:_RESULT_STUB_ARGS_PREVIEW_CHARS] + "…"
        first_id = self._identical_streak_first_call_id
        ref = f" (tool_call_id {first_id})" if first_id else ""
        stub = (
            f"[hermes note: this result is byte-identical to the {tool_name} "
            f"result earlier this turn{ref}. Refer to that result; it has not "
            f"changed. Args: {args_preview}]"
        )
        spill_path = self._persisted_result_paths.get(first_id) if first_id else None
        if spill_path:
            stub += f"\n[The referenced result was persisted to: {spill_path} — page through it with read_file if you need the full content.]"
        return stub

    def _check_loop_cap(
        self, tool_name: str, args: Mapping[str, Any], signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """
        调用前检查单轮硬上限；已达到上限时拒绝下一次调用，否则增加计数。

        delegate_task 的控制操作不创建子智能体，达到创建上限后仍可执行。
        """
        spec = _LOOP_CAPS.get(tool_name)
        if spec is None:
            return None
        cap_field, count_attr, code = spec
        cap, count = getattr(self.config.loop_caps, cap_field), getattr(self, count_attr)
        increment = 1 if tool_name == "web_search" else (_subagent_spawn_count(args) if cap else 0)
        if increment and cap and count >= cap:
            return self._decide("block", code, tool_name, count, signature, cap=cap)
        setattr(self, count_attr, count + increment)
        return None


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """
    为被阻止的调用构造 role=tool 使用的 JSON 结果。

    :param decision: 包含原因及诊断元数据的阻止决策
    :return: 不执行真实工具时返回的合成错误回执
    """
    return json.dumps({"error": decision.message, "guardrail": decision.to_metadata()}, ensure_ascii=False)


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """
    将循环警告或停止原因追加到当前工具结果末尾。

    :param result: 工具原始文本结果
    :param decision: 当前循环检测决策
    :return: 带运行时指导的结果；无有效警告或停止消息时原样返回
    """
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    return (result or "") + f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """
    根据具体工具生成重复失败后的恢复建议。
    """
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


def _ordinal(count: int) -> str:
    """
    格式化面向模型的循环提示计数。
    """
    return f"{count}{'th' if 11 <= count % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(count % 10, 'th')}"


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """
    将非对象参数按空参数处理，供检测器记录调用。
    """
    return args if isinstance(args, Mapping) else {}


# 代码内核的 execution_count 和 duration_seconds 每次执行都会变化，会掩盖真实重复。
# 仅移除 execute_code 结果中这些确定的元数据位置；其他工具的同名字段仍可能是业务输出。
def _without_execute_code_metadata(parsed: Any) -> Any:
    """
    仅忽略代码内核计数和执行耗时，保留有业务意义的代码输出。
    """
    if not isinstance(parsed, dict):
        return parsed
    cleaned = {k: v for k, v in parsed.items() if k != "duration_seconds"}
    kernel = cleaned.get("kernel")
    if isinstance(kernel, dict):
        cleaned["kernel"] = {k: v for k, v in kernel.items() if k != "execution_count"}
    return cleaned


def _result_hash(result: str | None, tool_name: str = "") -> str:
    """
    规范化结构化结果后计算哈希，保留其他工具的同名业务字段。
    """
    parsed = safe_json_loads(result or "")
    if parsed is None:
        return _sha256(result or "")
    if tool_name == "execute_code":
        parsed = _without_execute_code_metadata(parsed)
    return _sha256(_canonical_json(parsed))


_BOOL_WORDS = {w: True for w in ("1", "true", "yes", "on", "enabled")} | {w: False for w in ("0", "false", "no", "off", "disabled")}


def _as_bool(value: Any, default: bool) -> bool:
    """
    解析明确的布尔拼写，避免把表示假的非空文本当作真。
    """
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        return _BOOL_WORDS.get(value.strip().lower(), default)
    return default


def _int_at_least(value: Any, default: int, minimum: int) -> int:
    """
    非法、缺失或低于下限的数值回退到默认值；调用上限允许 0 表示禁用。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """
    计算本次 delegate_task 创建的子智能体数：非空批次取任务数，普通创建取 1，控制操作取 0。
    """
    if str(args.get("action") or "").strip().lower() in ("list", "steer", "stop"):
        return 0
    tasks = args.get("tasks")
    return len(tasks) if isinstance(tasks, list) and tasks else 1


def _sha256(value: str) -> str:
    # 网页结果可能包含未配对的 UTF-16 代理码点；surrogatepass 避免严格编码异常中断会话。
    """
    计算文本摘要，并兼容外部数据中未配对的 Unicode 代理码点。
    """
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
