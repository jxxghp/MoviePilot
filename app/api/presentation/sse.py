"""
SSE 事件编码、缓存控制与流式响应构造。
"""

import json
from collections.abc import AsyncIterable, Iterable
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_data_event(payload: dict[str, Any]) -> str:
    """
    把载荷编码为 OpenAI 使用的无名称 SSE 数据事件。

    :param payload: 可序列化为 JSON 的数据映射
    :return: 以空行结束的 data 事件文本，保留中文字符
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def encode_named_event(event: str, payload: dict[str, Any]) -> str:
    """
    把事件名和载荷编码为一个 SSE 事件，不增加业务响应包装。

    :param event: 协议定义的事件名称，由调用方提供
    :param payload: 可序列化为 JSON 的事件数据
    :return: 包含 event 和 data 行、以空行结束的事件文本
    """
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def build_sse_response(
    content: AsyncIterable[str] | Iterable[str],
    *,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """
    构造禁用代理缓冲的 SSE 响应。

    :param content: 同步或异步事件文本迭代器
    :param headers: 协议附加响应头；同名项覆盖默认头，None 表示使用默认值
    :return: 交给 HTTP 框架消费和关闭的流式响应
    """
    return StreamingResponse(
        content,
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **(headers or {})},
    )


def build_sse_error_response(payload: str) -> StreamingResponse:
    """
    用统一的流式传输策略返回单个错误事件。

    :param payload: 已按对应协议编码的完整 SSE 错误事件
    :return: 只产生该事件的流式响应
    """
    return build_sse_response(iter([payload]))
