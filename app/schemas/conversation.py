"""AI智能体对话记忆模型。

消息字段依赖 langchain_core，单独成模块，避免普通 schema 导入把 LangChain 带进未启用智能体的进程。
"""

from datetime import datetime
from typing import List, Optional

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ConversationMemory(BaseModel):
    """对话记忆模型"""

    session_id: str = Field(description="会话ID")
    user_id: Optional[str] = Field(default=None, description="用户ID")
    messages: List[BaseMessage] = Field(default_factory=list, description="消息列表")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")

    model_config = ConfigDict()

    @field_serializer("updated_at", when_used="json")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()
