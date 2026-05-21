"""让用户通过按钮进行选择的工具。"""

from typing import List, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool, ToolChain
from app.helper.interaction import (
    AgentInteractionOption,
    agent_interaction_manager,
)
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.message import ChannelCapabilityManager
from app.schemas.types import MessageChannel


class UserChoiceOptionInput(BaseModel):
    """单个按钮选项。"""

    label: str = Field(..., description="Text shown on the button")
    value: str = Field(
        ...,
        description="The exact content that will be sent back to the agent after the user clicks this button",
    )

    @model_validator(mode="after")
    def validate_option(self):
        label = str(self.label)
        value = str(self.value)
        if not label.strip():
            raise ValueError("label 不能为空")
        if not value.strip():
            raise ValueError("value 不能为空")
        return self


class AskUserChoiceInput(BaseModel):
    """按钮选择工具输入。"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why the agent needs the user to choose from buttons",
    )
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
        message = str(self.message)
        if not message.strip():
            raise ValueError("message 不能为空")
        if not self.options:
            raise ValueError("options 至少需要提供一个")
        return self


class AskUserChoiceTool(MoviePilotTool):
    name: str = "ask_user_choice"
    sends_message: bool = True
    description: str = (
        "Ask the user to choose from button options on channels that support interactive buttons. "
        "After the user clicks a button, the selected value will come back as the user's next message."
    )
    args_schema: Type[BaseModel] = AskUserChoiceInput
    require_admin: bool = False

    def get_tool_message(self, **kwargs) -> Optional[str]:
        message = kwargs.get("message", "") or ""
        if len(message) > 40:
            message = message[:40] + "..."
        return f"发送按钮选择: {message}"

    @staticmethod
    def _truncate_button_text(text: str, max_length: int) -> str:
        if max_length <= 0 or len(text) <= max_length:
            return text
        if max_length <= 3:
            return text[:max_length]
        return text[: max_length - 3] + "..."

    def _blocked_by_feedback_quality_gate(self) -> bool:
        """反馈 Issue 质量门槛拒绝后，禁止继续发按钮引导改写。

        这是对 ``feedback-issue`` skill 的工具层兜底：模型可能在
        ``submit_feedback_issue`` 返回 ``rejected_quality`` 后仍调用本工具，
        试图让用户选择“提供真实问题描述重新提交”。这会把测试 / 占位内容
        的拒绝结果变成绕过指导，因此同一轮 tool context 中直接拦截。
        """
        return bool(self._agent_context.get("feedback_issue_rejected_quality"))

    def _blocked_by_pending_feedback_confirmation(self) -> bool:
        """已经发出 ``prepare_feedback_issue`` 的预览按钮后，禁止再叠一层选择。

        Why: Issue #5807 实测中 deepseek 在 prepare 之后又自作主张调
        ``ask_user_choice``，给用户发了第二个「确认提交 ISSUE」按钮。
        两条按钮 → 两次 callback → agent 走两轮 → 同一条成功文案被发 3 次。
        从工具层硬拦：发现 ``reply_mode=feedback_issue_confirmation`` 直接拒绝。
        """
        return (
            self._agent_context.get("reply_mode") == "feedback_issue_confirmation"
        )

    async def run(
        self,
        message: str,
        options: List[UserChoiceOptionInput],
        title: Optional[str] = None,
        **kwargs,
    ) -> str:
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

        if self._blocked_by_pending_feedback_confirmation():
            logger.warning(
                "ask_user_choice blocked while feedback issue preview pending: "
                "session_id=%s",
                self._session_id,
            )
            return (
                "prepare_feedback_issue 已经发出确认按钮并在等待用户点击，"
                "不允许再叠加 ask_user_choice。请直接结束本轮，等待用户在"
                "现有按钮上点选。"
            )

        if not self._channel or not self._source:
            return "当前不在可回传消息的会话中，无法发起按钮选择"

        try:
            channel = MessageChannel(self._channel)
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
                label=option.label.strip(), value=option.value.strip()
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
                    "callback_data": (
                        f"agent_interaction:choice:{request.request_id}:{index}"
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

        await ToolChain().async_post_message(
            Notification(
                channel=channel,
                source=self._source,
                mtype=NotificationType.Agent,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message.strip(),
                buttons=buttons,
            )
        )

        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "button_choice"
        return f"已发送 {len(choice_options)} 个按钮选项，等待用户选择"
