"""请求与后台工作共用的关联 ID 上下文。"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, Iterator, Mapping

CORRELATION_ID_HEADER = "X-Request-ID"
MAX_CORRELATION_ID_LENGTH = 64
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CURRENT_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "moviepilot_correlation_id",
    default=None,
)


def normalize_correlation_id(candidate: str | None) -> str:
    """接受安全的调用方 ID；非法、超长或缺失值均替换为随机 ID。"""
    if candidate and _VALID_CORRELATION_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def get_correlation_id(default: str | None = None) -> str | None:
    """返回当前执行上下文中的关联 ID。"""
    return _CURRENT_CORRELATION_ID.get() or default


def set_correlation_id(correlation_id: str) -> Token[str | None]:
    """设置当前关联 ID，并返回供调用方精确恢复的 token。"""
    return _CURRENT_CORRELATION_ID.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """恢复设置关联 ID 之前的上下文。"""
    _CURRENT_CORRELATION_ID.reset(token)


@contextmanager
def correlation_scope(correlation_id: str | None) -> Iterator[str | None]:
    """在当前同步或异步任务作用域内绑定并自动恢复关联 ID。"""
    if correlation_id is None:
        yield None
        return
    token = set_correlation_id(correlation_id)
    try:
        yield correlation_id
    finally:
        reset_correlation_id(token)


def with_correlation_header(headers: Mapping[str, str] | None) -> dict[str, str]:
    """复制请求头并在调用方未显式指定时加入当前关联 ID。"""
    result = dict(headers or {})
    if any(key.lower() == CORRELATION_ID_HEADER.lower() for key in result):
        return result
    correlation_id = get_correlation_id()
    if correlation_id:
        result[CORRELATION_ID_HEADER] = correlation_id
    return result


def call_with_correlation(
    correlation_id: str | None,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """从可序列化参数恢复关联 ID 后调用函数，供子进程入口使用。"""
    with correlation_scope(correlation_id):
        return func(*args, **kwargs)
