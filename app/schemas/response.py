
from pydantic import BaseModel


class Response(BaseModel):
    # 状态
    success: bool
    # 消息文本
    message: str | None = None
    # 数据
    data: dict | list | None = {}
