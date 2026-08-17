"""让用户通过按钮进行选择的工具。"""

from typing import List, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.messaging.agent import (
    AgentInteractionOption,
    agent_interaction_manager,
    build_agent_choice_callback,
)
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.message import MessageType
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import NotificationChannel


class UserChoiceOptionInput(BaseModel):
    """单个按钮选项。"""

    label: str = Field(..., description="Text shown on the button")
    value: str = Field(
        ...,
        description="The exact content that will be sent back to the agent after the user clicks this button",
    )

    @model_validator(mode="after")
    def validate_option(self):
        """校验按钮选项的文案和值不能为空。"""
        label = str(self.label)
        value = str(self.value)
        if not label.strip():
            raise ValueError("label 不能为空")
        if not value.strip():
            raise ValueError("value 不能为空")
        return self


class AskUserChoiceInput(BaseModel):
    """按钮选择工具输入。"""

    message: str = Field(
        ...,
        description="Question or prompt shown to the user together with the buttons",
    )
    title: Optional[str] = Field(
        None,
        description="Optional short title displayed above the question",
    )
    options: List[UserChoiceOptionInput] = Field(
        ...,
        description="Button options to show to the user",
    )

    @model_validator(mode="after")
    def validate_payload(self):
        """校验按钮选择工具必须提供问题和选项。"""
        message = str(self.message)
        if not message.strip():
            raise ValueError("message 不能为空")
        if not self.options:
            raise ValueError("options 至少需要提供一个")
        return self


class AskUserChoiceTool(MoviePilotTool):
    """发送按钮选择并让当前 Agent 轮次等待用户回调消息。"""

    name: str = "ask_user_choice"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Message,
        ToolTag.UserInteraction,
        ToolTag.TerminalResponse,
    ]
    sends_message: bool = True
    return_direct: bool = True
    description: str = (
        "Ask the user to choose from button options on channels that support interactive buttons. "
        "This is a terminal interaction tool: put the full question and all options in this call, "
        "then stop the current turn. After the user clicks a button, the selected value will come "
        "back as the user's next message. Do not also send the same question as plain text."
    )
    args_schema: Type[BaseModel] = AskUserChoiceInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成工具执行提示文案。"""
        message = kwargs.get("message", "") or ""
        if len(message) > 40:
            message = message[:40] + "..."
        return f"发送按钮选择: {message}"

    @staticmethod
    def _truncate_button_text(text: str, max_length: int) -> str:
        """按渠道限制截断按钮文案。"""
        if max_length <= 0 or len(text) <= max_length:
            return text
        if max_length <= 3:
            return text[:max_length]
        return text[: max_length - 3] + "..."

    def _blocked_by_feedback_quality_gate(self) -> bool:
        """反馈 Issue 质量门槛拒绝后，禁止继续发按钮引导改写。

        这是对 ``feedback-issue`` skill 的历史兜底：如果同一轮上下文已经
        标记反馈内容被质量门槛拒绝，就不能再用按钮诱导用户把测试 / 占位
        内容改写成“真实问题”。
        """
        return bool(self._agent_context.get("feedback_issue_rejected_quality"))

    async def run(
        self,
        message: str,
        options: List[UserChoiceOptionInput],
        title: Optional[str] = None,
        **kwargs,
    ) -> str:
        """
        发送按钮选择消息，并登记待回调的交互上下文。

        :param message: 展示给用户的问题
        :param options: 可点击的选项列表
        :param title: 可选标题
        :return: 工具执行结果描述
        """
        if self._blocked_by_feedback_quality_gate():
            logger.warning(
                "ask_user_choice blocked after feedback issue rejected_quality: "
                "session_id=%s",
                self._session_id,
            )
            return (
                "反馈 Issue 已被质量门槛拒绝，不能继续发送按钮引导用户改写或重新提交。"
                "请直接结束本次反馈流程。"
            )

        if not self._channel or not self._source:
            return "当前不在可回传消息的会话中，无法发起按钮选择"

        try:
            channel = NotificationChannel(self._channel)
        except ValueError:
            return f"不支持的消息渠道: {self._channel}"

        if not (
            ChannelCapabilityManager.supports_buttons(channel)
            and ChannelCapabilityManager.supports_callbacks(channel)
        ):
            return f"当前渠道 {channel.value} 不支持按钮选择"

        max_per_row = 1
        max_rows = ChannelCapabilityManager.get_max_button_rows(channel)
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_options = max_per_row * max_rows
        if len(options) > max_options:
            return f"当前渠道最多支持 {max_options} 个按钮选项"

        choice_options = [
            AgentInteractionOption(
                label=option.label.strip(),
                value=option.value.strip(),
            )
            for option in options
        ]
        request = agent_interaction_manager.create_request(
            session_id=self._session_id,
            user_id=str(self._user_id),
            channel=channel.value,
            source=self._source,
            username=self._username,
            title=title,
            prompt=message.strip(),
            options=choice_options,
        )

        buttons = []
        current_row = []
        for index, option in enumerate(choice_options, start=1):
            current_row.append(
                {
                    "text": self._truncate_button_text(option.label, max_text_length),
                    "callback_data": build_agent_choice_callback(
                        request.request_id, index
                    ),
                }
            )
            if len(current_row) >= max_per_row:
                buttons.append(current_row)
                current_row = []
        if current_row:
            buttons.append(current_row)

        logger.info(
            "执行工具: %s, channel=%s, session_id=%s, options=%s",
            self.name,
            channel.value,
            self._session_id,
            len(choice_options),
        )

        await self.send_message(
            Message(
                channel=channel,
                source=self._source,
                mtype=MessageType.Agent,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message.strip(),
                buttons=buttons,
                save_history=False,
            )
        )

        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "button_choice"
        return f"已发送 {len(choice_options)} 个按钮选项，等待用户选择"
