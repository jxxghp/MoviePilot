"""Agent 轻量公共合同，不触发模型、工具或编排运行时加载。"""

import uuid
from datetime import datetime
from typing import Any, Optional

from app.schemas.types import ReplyMode


def build_display_message(
    role: str,
    content: str = "",
    attachments: Optional[list[dict]] = None,
    status: str = "done",
) -> dict[str, Any]:
    """构造前后端共享的 Agent 会话展示消息。"""
    normalized_content = content or ""
    return {
        "id": f"{role}-{uuid.uuid4().hex}",
        "role": role,
        "content": normalized_content,
        "createdAt": int(datetime.now().timestamp() * 1000),
        "status": status,
        "tools": [],
        "segments": (
            [{"type": "text", "content": normalized_content}]
            if normalized_content
            else []
        ),
        "attachments": attachments or [],
        "choices": [],
    }


__all__ = ["ReplyMode", "build_display_message"]
