"""发送消息工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import MessageType


class SendMessageInput(BaseModel):
    """发送消息工具的输入参数模型"""

    message: Optional[str] = Field(
        None,
        description="The message content to send to the user (should be clear and informative)",
    )
    title: Optional[str] = Field(
        None,
        description="Title of the message, a short summary of the message content",
    )
    image_url: Optional[str] = Field(
        None,
        description="Optional image URL to send together with the message on channels that support images (such as Telegram and Slack)",
    )

    @model_validator(mode="after")
    def validate_payload(self) -> "SendMessageInput":
        """校验消息内容和可选格式参数。"""
        if not self.message and not self.title and not self.image_url:
            raise ValueError("message、title、image_url 至少需要提供一个")
        return self


class SendMessageTool(MoviePilotTool):
    """发送普通通知消息给当前用户。"""

    name: str = "send_message"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Message,
        ToolTag.Admin,
        ToolTag.TerminalResponse,
    ]
    sends_message: bool = True
    return_direct: bool = True
    description: str = (
        "Send notification message to the user through configured notification channels "
        "(Telegram, Slack, WeChat, etc.). Supports optional image_url on channels that can "
        "send images. This is a terminal response tool: after it sends the user-facing "
        "message, do not send another final text reply with the same content."
    )
    args_schema: Type[BaseModel] = SendMessageInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据消息参数生成友好的提示消息"""
        message = kwargs.get("message", "") or ""
        title = kwargs.get("title") or ""
        image_url = kwargs.get("image_url")

        # 截断过长的消息
        if len(message) > 50:
            message = message[:50] + "..."

        if title and image_url:
            return f"发送图文消息: [{title}] {message}"
        if title:
            return f"发送消息: [{title}] {message}"
        if image_url:
            return f"发送图片消息: {message}"
        return f"发送消息: {message}"

    async def run(
        self,
        message: Optional[str] = None,
        title: Optional[str] = None,
        image_url: Optional[str] = None,
        **kwargs,
    ) -> str:
        """发送消息到当前会话渠道。"""
        title = title or ("图片" if image_url and not message else "")
        text = message or ""

        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, message={text}, "
            f"image_url={image_url}"
        )
        try:
            await self.send_message(
                Message(
                    channel=self._channel,
                    source=self._source,
                    mtype=MessageType.Other,
                    userid=self._user_id,
                    username=self._username,
                    title=title,
                    text=text,
                    image=image_url,
                    save_history=False,
                )
            )
            self._agent_context["user_reply_sent"] = True
            self._agent_context["reply_mode"] = "send_message"
            return "消息已发送"
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return f"发送消息时发生错误: {str(e)}"
