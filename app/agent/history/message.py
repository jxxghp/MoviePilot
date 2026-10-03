"""原始消息的稳定身份与可归档文字投影。"""

import hashlib
import json
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage

from app.agent.tools.result import TOOL_OBSERVATION_MARKER
from app.application.messaging.recall import RecallMessage


def legacy_identity(session_id: str, position: int, message: BaseMessage) -> str:
    """缺失旧消息 ID 时由来源位置和内容生成稳定标识，恢复与回填使用同一算法。"""
    payload = json.dumps([session_id, position, message.type, message.content], ensure_ascii=False, sort_keys=True)
    return 'legacy:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


def evidence(message: BaseMessage) -> RecallMessage | None:
    """提取完整文字与工具调用，跳过合成摘要和请求临时投影。"""
    if message.type not in {'human', 'ai', 'tool'}:
        return None
    if message.additional_kwargs.get('lc_source') == 'summarization' or message.additional_kwargs.get(TOOL_OBSERVATION_MARKER):
        return None
    if not message.id:
        message.id = uuid4().hex
    content = message.content
    if isinstance(content, list):
        content = '\n'.join(block if isinstance(block, str) else str(block.get('text', '')) for block in content
                            if isinstance(block, str) or isinstance(block, dict) and block.get('type') == 'text')
    calls = json.dumps(message.tool_calls, ensure_ascii=False) if isinstance(message, AIMessage) and message.tool_calls else ''
    return RecallMessage(message_id=message.id, role={'human': 'user', 'ai': 'assistant', 'tool': 'tool'}[message.type],
                         content=content, tool_calls=calls, tool_call_id=str(getattr(message, 'tool_call_id', '') or ''),
                         tool_name=str(message.name or ''), tool_status=str(getattr(message, 'status', '') or ''))
