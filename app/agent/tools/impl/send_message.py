"""发送消息工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.log import logger


class SendMessageInput(BaseModel):
    """发送消息工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    message: Optional[str] = Field(
        None,
        description="The message content to send to the user (should be clear and informative)",
    )
    message_type: Optional[str] = Field(
        None,
        description="Title of the message, a short summary of the message content",
    )
    image_url: Optional[str] = Field(
        None,
        description="Optional image URL to send together with the message on channels that support images (such as Telegram and Slack)",
    )

    @model_validator(mode="after")
    def validate_payload(self):
        if not self.message and not self.message_type and not self.image_url:
            raise ValueError("message、message_type、image_url 至少需要提供一个")
        return self


class SendMessageTool(MoviePilotTool):
    name: str = "send_message"
    description: str = "Send notification message to the user through configured notification channels (Telegram, Slack, WeChat, etc.). Supports optional image_url on channels that can send images. Used to inform users about operation results, errors, important updates, or proactively send a relevant image."
    args_schema: Type[BaseModel] = SendMessageInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据消息参数生成友好的提示消息"""
        message = kwargs.get("message", "") or ""
        title = kwargs.get("message_type") or ""
        image_url = kwargs.get("image_url")

        # 截断过长的消息
        if len(message) > 50:
            message = message[:50] + "..."

        if title and image_url:
            return f"正在发送图文消息: [{title}] {message}"
        if title:
            return f"正在发送消息: [{title}] {message}"
        if image_url:
            return f"正在发送图片消息: {message}"
        return f"正在发送消息: {message}"

    async def run(
        self,
        message: Optional[str] = None,
        message_type: Optional[str] = None,
        image_url: Optional[str] = None,
        **kwargs,
    ) -> str:
        title = message_type or ("图片" if image_url and not message else "")
        text = message or ""
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, message={text}, image_url={image_url}"
        )
        try:
            await self.send_tool_message(text, title=title, image=image_url)
            return "消息已发送"
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return f"发送消息时发生错误: {str(e)}"
