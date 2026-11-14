"""
发送消息工具
"""

from typing import Any, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import MessageType


class SendMessageInput(BaseModel):
    """
    发送消息工具的输入参数模型
    """

    message: Optional[str] = Field(
        None,
        description="发送给用户的清晰完整消息正文",
    )
    title: Optional[str] = Field(
        None,
        description="简短概括正文的消息标题",
    )
    image_url: Optional[str] = Field(
        None,
        description="支持图片的渠道中随消息发送的可选图片 URL，例如 Telegram 或 Slack",
    )
    rich_message: Optional[str] = Field(
        None,
        description=(
            "采用 GitHub 风格 Markdown 的完整 Telegram 富文本正文。"
            "Telegram 用它替代 message、title 和 image_url；"
            "其他渠道在 message 为空时用作纯文本回退"
        ),
    )

    @model_validator(mode="after")
    def validate_payload(self) -> "SendMessageInput":
        """
        校验消息内容和可选格式参数。
        """
        if (
            not self.message
            and not self.title
            and not self.image_url
            and not self.rich_message
        ):
            raise ValueError("message、title、image_url、rich_message 至少需要提供一个")
        return self


class SendMessageTool(MoviePilotTool):
    """
    发送普通通知消息给当前用户。
    """

    name: str = Field(
        default="send_message",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Write, ToolTag.Message, ToolTag.Admin, ToolTag.TerminalResponse],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    sends_message: bool = Field(
        default=True,
        description="工具是否直接向当前用户发送消息",
    )
    return_direct: bool = Field(
        default=True,
        description="是否直接返回工具结果并结束本轮智能体响应",
    )
    description: str = Field(
        default=(
            "通过已配置的通知渠道（Telegram、Slack、微信等）向用户发送消息。"
            "支持图片的渠道可传 image_url。"
            "Telegram 的结构化最终回复优先使用 rich_message，并以 GitHub 风格 Markdown 提供完整内容。"
            "本工具直接完成用户回复；"
            "发送后不要再发送内容相同的最终文本。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=SendMessageInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        根据消息参数生成友好的提示消息
        """
        rich_message = kwargs.get("rich_message") or ""
        message = kwargs.get("message", "") or rich_message
        title = kwargs.get("title") or ""
        image_url = kwargs.get("image_url")

        # 截断过长的消息
        if len(message) > 50:
            message = message[:50] + "..."

        if rich_message:
            return f"发送富文本消息: {message}"
        if title and image_url:
            return f"发送图文消息: [{title}] {message}"
        if title:
            return f"发送消息: [{title}] {message}"
        if image_url:
            return f"发送图片消息: {message}"
        return f"发送消息: {message}"

    async def run(  # type: ignore[override]
        self,
        message: Optional[str] = None,
        title: Optional[str] = None,
        image_url: Optional[str] = None,
        rich_message: Optional[str] = None,
        **kwargs: object,
    ) -> str:
        """
        发送消息到当前会话渠道。
        """
        title = title or ("图片" if image_url and not message and not rich_message else "")
        text = message or rich_message or ""

        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, message={text}, "
            f"image_url={image_url}, rich_message={bool(rich_message)}"
        )
        try:
            await self.send_message(
                Message.model_validate({
                    "channel": self._channel,
                    "source": self._source,
                    "mtype": MessageType.Other,
                    "userid": self._user_id,
                    "username": self._username,
                    "title": title,
                    "text": text,
                    "image": image_url,
                    "rich_message": rich_message,
                    "save_history": False,
                })
            )
            self._agent_context["user_reply_sent"] = True
            self._agent_context["reply_mode"] = "send_message"
            return "消息已发送"
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return f"发送消息时发生错误: {str(e)}"
