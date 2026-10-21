"""
发送语音消息工具。
"""
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.llm.capability import AgentCapabilityManager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.message import Message
from app.schemas.types import MessageType


class SendVoiceMessageInput(BaseModel):
    """
    发送语音消息工具输入。
    """

    message: str = Field(
        ...,
        description="需要通过语音回复给当前用户的完整正文",
    )


class SendVoiceMessageTool(MoviePilotTool):
    """
    发送 Agent 语音回复的工具。
    """

    name: str = Field(
        default="send_voice_message",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Write, ToolTag.Message, ToolTag.TerminalResponse],
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
            "向当前用户发送语音回复。"
            "仅在用户明确要求语音，或语音播放明显更合适时使用。"
            "渠道不支持语音或 TTS 不可用时，自动回退为发送相同内容的纯文本。"
            "本工具直接完成用户回复：在 message 中提供完整正文；"
            "执行后不要再发送相同文本或调用 send_message 重复发送。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=SendVoiceMessageInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        生成语音回复工具的执行提示。
        """
        message = kwargs.get("message") or ""
        if len(message) > 40:
            message = message[:40] + "..."
        return f"发送语音回复: {message}"

    async def run(self, message: str, **kwargs: Any) -> str:  # type: ignore[override]
        """
        合成语音并发送到当前对话渠道，不支持时回退为文字。
        """
        if not message:
            return "语音回复内容不能为空"

        voice_path = None
        used_voice = False
        channel = self._channel or ""
        reply_mode = AgentCapabilityManager.resolve_reply_mode(
            channel=channel,
            source=self._source,
        )
        fallback_reason = "当前渠道不支持语音回复"
        if not AgentCapabilityManager.supports_audio_output():
            fallback_reason = "当前未启用音频输出"
        if (
            reply_mode == AgentCapabilityManager.REPLY_MODE_NATIVE
            and AgentCapabilityManager.is_audio_output_available()
        ):
            voice_file = await self.run_blocking(
                "default",
                AgentCapabilityManager.synthesize_speech, message
            )
            if voice_file:
                voice_path = str(voice_file)
                used_voice = True
        elif reply_mode == AgentCapabilityManager.REPLY_MODE_NATIVE:
            fallback_reason = "当前未配置可用的语音合成能力"

        logger.info(
            f"执行工具: {self.name}, channel={channel}, "
            f"use_voice={used_voice}, text_len={len(message)}"
        )

        await self.send_message(
            Message.model_validate({
                "channel": self._channel,
                "source": self._source,
                "mtype": MessageType.Agent,
                "userid": self._user_id,
                "username": self._username,
                "text": message,
                "voice_path": voice_path,
                "voice_caption": (
                    message
                    if voice_path and get_runtime_setting('AUDIO_OUTPUT_INCLUDE_TEXT')
                    else None
                ),
                "save_history": False,
            })
        )
        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "voice" if used_voice else "text_fallback"

        if used_voice:
            return "语音回复已发送"
        return f"{fallback_reason}，已自动回退为文字回复"
