"""提供统一的错误文本规范化，不改变业务层已经生成的错误含义。"""

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


def public_error_message(
    error: Optional[object],
    *,
    context: PublicErrorContext = "generic",
    fallback: Optional[str] = None,
) -> str:
    """返回业务层提供的错误文案，仅清理空白并为空值提供兜底。"""
    if error is None:
        return fallback or _CONTEXT_FALLBACKS[context]
    message = " ".join(str(error).split())
    return message or fallback or _CONTEXT_FALLBACKS[context]
