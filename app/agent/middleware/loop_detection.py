"""MoviePilot 智能体循环检测中间件。

用于识别并中断模型陷入的重复循环，避免"循环输出相同内容"或"反复调用
同一工具"导致的无意义执行。

检测两类循环：
1. 重复工具调用：同一工具名 + 相同参数在短时间内被连续调用多次。
2. 重复输出：模型连续多次生成相同或高度相似的回复文本。

达到阈值时抛出 `AgentLoopDetectedError`，由 orchestrator 专门捕获并终止
本轮 Agent 执行，同时向用户返回友好提示。
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage

from app.runtime.log import logger


class AgentLoopDetectedError(Exception):
    """检测到模型陷入重复循环时抛出的异常。"""

    def __init__(self, message: str, *, loop_type: str = "unknown") -> None:
        super().__init__(message)
        self.loop_type = loop_type


# 连续重复多少次判定为循环
DEFAULT_MAX_REPEATED_TOOL_CALLS = 3
DEFAULT_MAX_REPEATED_OUTPUTS = 3
# 输出文本相似度阈值（0~1），用于判断两条文本是否"高度相似"
DEFAULT_OUTPUT_SIMILARITY_THRESHOLD = 0.95


class LoopDetectionMiddleware(AgentMiddleware):
    """检测并中断模型重复循环的中间件。

    状态保存在中间件实例上，并在每次 Agent 执行开始时（``abefore_agent``）
    重置，避免跨用户请求残留。
    """

    def __init__(
        self,
        *,
        max_repeated_tool_calls: int = DEFAULT_MAX_REPEATED_TOOL_CALLS,
        max_repeated_outputs: int = DEFAULT_MAX_REPEATED_OUTPUTS,
        output_similarity_threshold: float = DEFAULT_OUTPUT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._max_repeated_tool_calls = max_repeated_tool_calls
        self._max_repeated_outputs = max_repeated_outputs
        self._output_similarity_threshold = output_similarity_threshold
        self._tool_call_history: list[str] = []
        self._output_history: list[str] = []

    # ------------------------------------------------------------------
    # 状态重置
    # ------------------------------------------------------------------
    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Any,  # noqa: ARG002
    ) -> None:
        """每次 Agent 执行开始时重置循环检测状态。"""
        self._tool_call_history = []
        self._output_history = []
        return None

    # ------------------------------------------------------------------
    # 工具调用签名规范化
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_tool_call_signature(tool_name: str, args: Any) -> str:
        """把工具名和参数规范化为可比较的签名串。"""
        try:
            args_json = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_json = str(args)
        return f"{tool_name}::{args_json}"

    @staticmethod
    def _is_repeating(history: list[str], max_repeats: int) -> bool:
        """判断最近 ``max_repeats`` 条记录是否完全相同。"""
        if len(history) < max_repeats:
            return False
        recent = history[-max_repeats:]
        return len(set(recent)) == 1

    # ------------------------------------------------------------------
    # 重复工具调用检测
    # ------------------------------------------------------------------
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在工具执行前检测重复调用，命中循环则中断。"""
        tool_call = request.tool_call or {}
        tool_name = str(tool_call.get("name") or getattr(request.tool, "name", None) or "unknown")
        args = tool_call.get("args") or {}
        signature = self._normalize_tool_call_signature(tool_name, args)

        self._tool_call_history.append(signature)
        if self._is_repeating(self._tool_call_history, self._max_repeated_tool_calls):
            logger.warning(
                f"检测到重复工具调用循环: tool={tool_name}, "
                f"连续 {self._max_repeated_tool_calls} 次相同调用"
            )
            raise AgentLoopDetectedError(
                f"检测到重复工具调用循环（工具 {tool_name} 连续 "
                f"{self._max_repeated_tool_calls} 次相同调用），已中断。",
                loop_type="repeated_tool_call",
            )

        return await handler(request)

    # ------------------------------------------------------------------
    # 重复输出检测
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_output_text(response: ModelResponse[Any]) -> str:
        """从模型响应中提取纯文本输出。"""
        for msg in reversed(response.result):
            if isinstance(msg, AIMessage) and msg.content:
                content = msg.content
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict) and item.get("type") == "text":
                            parts.append(str(item.get("text", "")))
                    return "".join(parts)
                return str(content)
        return ""

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """计算两条文本的相似度（0~1）。

        使用字符 bigram 集合的 Jaccard 相似度，并叠加长度差异惩罚。
        bigram 覆盖完整文本内容，能区分"相同前缀但后续不同"的文本，
        同时计算开销为 O(n)，避免 LCS 在长文本上的 O(n*m) 开销。
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        def _bigrams(s: str) -> set:
            if len(s) <= 1:
                return {s}
            return {s[i:i + 2] for i in range(len(s) - 1)}

        bigrams_a = _bigrams(a)
        bigrams_b = _bigrams(b)
        if not bigrams_a or not bigrams_b:
            return 0.0

        intersection = len(bigrams_a & bigrams_b)
        union = len(bigrams_a | bigrams_b)
        jaccard = intersection / union if union else 0.0

        # 长度差异惩罚：长度差异越大，相似度越低
        len_ratio = min(len(a), len(b)) / max(len(a), len(b))
        return jaccard * len_ratio

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """在模型输出后检测重复文本，命中循环则中断。"""
        response = await handler(request)
        text = self._extract_output_text(response)
        if not text:
            return response

        self._output_history.append(text)
        if len(self._output_history) >= self._max_repeated_outputs:
            recent = self._output_history[-self._max_repeated_outputs:]
            # 全部完全相同
            if len(set(recent)) == 1:
                logger.warning(
                    f"检测到重复输出循环: 连续 {self._max_repeated_outputs} 次相同文本"
                )
                raise AgentLoopDetectedError(
                    f"检测到重复输出循环（连续 {self._max_repeated_outputs} 次相同内容），"
                    "已中断。",
                    loop_type="repeated_output",
                )
            # 高度相似（相邻两两相似度均超过阈值）
            all_similar = all(
                self._similarity(recent[i], recent[i + 1])
                >= self._output_similarity_threshold
                for i in range(len(recent) - 1)
            )
            if all_similar:
                logger.warning(
                    f"检测到高度相似的重复输出循环: 连续 {self._max_repeated_outputs} 次"
                )
                raise AgentLoopDetectedError(
                    f"检测到高度相似的重复输出循环（连续 {self._max_repeated_outputs} 次"
                    "内容高度相似），已中断。",
                    loop_type="repeated_output",
                )

        return response
