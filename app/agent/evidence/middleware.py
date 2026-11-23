"""
在工具回执与主模型请求之间临时投影证据，不让工具结果返回原文。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage

from app.agent.evidence.scope import (
    EVIDENCE_MARKER,
    EVIDENCE_RECEIPT,
    EVIDENCE_UNAVAILABLE,
    evidence_call,
    evidence_in_use,
    materialize_evidence,
)


class EphemeralEvidenceError(RuntimeError):
    """
    临时资料请求失败的安全说明，不串接可能包含原文的上游异常。
    """


class EphemeralEvidenceMiddleware(AgentMiddleware):
    """
    工具只提交安全回执，原文仅在主模型最终压缩预算内临时展开。
    """

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]) -> Any:
        """
        在工具自身格式化之前绑定发布入口，通用结果缓存和策略日志只看到回执。

        :param request: 已通过外层宿主策略的真实工具调用
        :param handler: ToolNode 的执行入口，必须返回标准工具消息
        :return: 带本地引用的安全回执，或未发布资料的原工具结果
        :raises EphemeralEvidenceError: 已发布资料的工具未正常生成回执
        """
        with evidence_call(str(request.tool_call.get("id") or ""), str(request.tool_call.get("name") or "")) as reference:
            try:
                result = await handler(request)
            except Exception:
                if reference() is not None:
                    raise EphemeralEvidenceError("临时知识工具执行未完成，请重新检索") from None
                raise
            identity = reference()
            if identity is None:
                return result
            if not isinstance(result, ToolMessage):
                raise EphemeralEvidenceError("临时知识工具未返回标准回执，已中止执行")
            return result.model_copy(update={
                "content": EVIDENCE_RECEIPT if result.status == "success" else EVIDENCE_UNAVAILABLE,
                "artifact": None, "response_metadata": {},
                "additional_kwargs": {EVIDENCE_MARKER: identity} if result.status == "success" else {},
            })

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """
        同步请求计入正文预算，但不把请求副本写进图状态或异常日志。

        :param request: 图状态投影出的最终主模型请求
        :param handler: 包含压缩与预算检查的内层请求处理器
        :return: 模型生成的响应，临时请求副本不作为状态更新
        :raises EphemeralEvidenceError: 使用临时资料后的请求未完成
        """
        try:
            return handler(request.override(messages=[materialize_evidence(message) for message in request.messages]))
        except Exception:
            if evidence_in_use():
                raise EphemeralEvidenceError("临时知识推理未完成，请重新检索后重试") from None
            raise

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """
        异步请求使用同一投影合同；取消由执行轮所有者清空容器。

        :param request: 图状态投影出的最终主模型请求
        :param handler: 包含压缩与预算检查的异步内层处理器
        :return: 主模型响应，取消继续向执行轮所有者传播
        :raises EphemeralEvidenceError: 使用临时资料后的请求未完成
        """
        try:
            return await handler(request.override(messages=[materialize_evidence(message) for message in request.messages]))
        except Exception:
            if evidence_in_use():
                raise EphemeralEvidenceError("临时知识推理未完成，请重新检索后重试") from None
            raise
