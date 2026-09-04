"""把内部错误转换为可直接展示给用户的简短文案。"""

from typing import Literal, Optional

PublicErrorContext = Literal[
    "generic",
    "transfer",
    "subscription",
    "message",
    "outbox",
]

_CONTEXT_FALLBACKS: dict[PublicErrorContext, str] = {
    "generic": "操作失败，请稍后重试",
    "transfer": "整理失败，请刷新后重试",
    "subscription": "订阅操作失败，请刷新后重试",
    "message": "消息处理失败，请稍后重试",
    "outbox": "后台任务暂未完成，系统会自动重试",
}

_TECHNICAL_MARKERS = (
    "outbox",
    "durable",
    "checkpoint",
    "provider_pending",
    "schema_version",
    "operation_id",
    "operation id",
    "attempt_token",
    "attempt token",
    "lease_token",
    "transferinfo",
    "dispatcher",
    "traceback",
    "runtimeerror",
    "valueerror",
    "typeerror",
    "keyerror",
    "attributeerror",
    "operationalerror",
    "connectionerror",
    "timeout",
    "client error",
    "api error",
    "connecterror",
    "connect error",
    "connection",
    "errno",
    "http ",
    "http/",
    "max retries",
    "retryerror",
    "response status",
    "sslerror",
    "ssl error",
    "status code",
    "too many requests",
    "server error",
    "for url",
    "object at 0x",
    "no streaming chunk",
    "streaming chunk",
    "failed",
    "检查点",
    "步骤意图",
    "意图",
    "事实",
    "证据",
    "租约",
    "副作用",
    "执行凭证",
    "指纹",
    "回执",
)


def _normalize_error(error: Optional[object]) -> str:
    """压缩异常中的换行和多余空白，避免内部格式污染界面。"""
    if error is None:
        return ""
    return " ".join(str(error).split())


def _contains_technical_marker(message: str) -> bool:
    """判断文案是否包含只适合日志或开发者排查的实现术语。"""
    normalized = message.lower()
    return any(marker.lower() in normalized for marker in _TECHNICAL_MARKERS)


def _context_fallback(
    context: PublicErrorContext,
    message: str,
    fallback: str | None,
) -> str:
    """根据业务上下文选择不泄露内部细节的兜底文案。"""
    if fallback:
        return fallback
    normalized = message.lower()
    if "订阅" in message or context == "subscription":
        return _CONTEXT_FALLBACKS["subscription"]
    if "智能助手" in message or "ai智能体" in normalized:
        return "智能助手执行失败，请稍后重试"
    if "整理" in message or context == "transfer":
        return _CONTEXT_FALLBACKS["transfer"]
    if "outbox" in normalized or "durable" in normalized or context == "outbox":
        return _CONTEXT_FALLBACKS["outbox"]
    return _CONTEXT_FALLBACKS[context]


def public_error_message(
    error: Optional[object],
    *,
    context: PublicErrorContext = "generic",
    fallback: Optional[str] = None,
) -> str:
    """将内部异常或状态转换为人类可理解的前台提示。

    只有明确识别为内部实现信息的文本才会被替换；普通的业务提示会原样保留，
    这样既能统一异常出口，又不会丢失“目录不存在”等可执行的处理建议。
    原始异常应继续写入日志或结构化诊断字段，不应作为本函数的返回值直接展示。
    """
    message = _normalize_error(error)
    if not message:
        return fallback or _CONTEXT_FALLBACKS[context]

    if _contains_technical_marker(message):
        return _context_fallback(context, message, fallback)
    return message
