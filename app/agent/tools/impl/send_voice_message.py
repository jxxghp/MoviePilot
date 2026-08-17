"""发送语音消息工具。"""
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.llm.capability import AgentCapabilityManager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.config import settings
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.message import MessageType


class SendVoiceMessageInput(BaseModel):
    """发送语音消息工具输入。"""

    message: str = Field(
        ...,
        description="The spoken content to send back to the user",
    )


class SendVoiceMessageTool(MoviePilotTool):
    """发送 Agent 语音回复的工具。"""

    name: str = "send_voice_message"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Message,
        ToolTag.TerminalResponse,
    ]
    sends_message: bool = True
    return_direct: bool = True
    description: str = (
        "Send a voice reply to the current user. Use this only when the user explicitly asks for "
        "a voice reply or when spoken playback is clearly better than plain text. On channels "
        "without voice support or when TTS is unavailable, it automatically falls back to sending "
        "the same content as plain text. This is a terminal response tool: put the complete "
        "user-facing reply in `message`; after this tool runs, do not send another text reply "
        "or call `send_message` with the same content."
    )
    args_schema: Type[BaseModel] = SendVoiceMessageInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成语音回复工具的执行提示。"""
        message = kwargs.get("message") or ""
        if len(message) > 40:
            message = message[:40] + "..."
        return f"发送语音回复: {message}"

    async def run(self, message: str, **kwargs) -> str:
        """合成语音并发送到当前对话渠道，不支持时回退为文字。"""
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
            Message(
                channel=self._channel,
                source=self._source,
                mtype=MessageType.Agent,
                userid=self._user_id,
                username=self._username,
                text=message,
                voice_path=voice_path,
                voice_caption=(
                    message
                    if voice_path and settings.AUDIO_OUTPUT_INCLUDE_TEXT
                    else None
                ),
                save_history=False,
            )
        )
        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "voice" if used_voice else "text_fallback"

        if used_voice:
            return "语音回复已发送"
        return f"{fallback_reason}，已自动回退为文字回复"
