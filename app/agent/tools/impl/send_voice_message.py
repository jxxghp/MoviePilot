"""发送语音消息工具。"""

import asyncio
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool, ToolChain
from app.core.config import settings
from app.helper.voice import VoiceHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.types import MessageChannel


class SendVoiceMessageInput(BaseModel):
    """发送语音消息工具输入。"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why a voice reply is the best fit in the current context",
    )
    message: str = Field(
        ...,
        description="The spoken content to send back to the user",
    )


class SendVoiceMessageTool(MoviePilotTool):
    name: str = "send_voice_message"
    description: str = (
        "Send a voice reply to the current user. Prefer this when the user sent a voice message "
        "or when spoken playback is more natural. On channels without voice support or when TTS "
        "is unavailable, it automatically falls back to sending the same content as plain text."
    )
    args_schema: Type[BaseModel] = SendVoiceMessageInput
    require_admin: bool = False

    def get_tool_message(self, **kwargs) -> Optional[str]:
        message = kwargs.get("message") or ""
        if len(message) > 40:
            message = message[:40] + "..."
        return f"正在发送语音回复: {message}"

    def _supports_real_voice_reply(self) -> bool:
        channel = self._channel or ""
        if channel == MessageChannel.Telegram.value:
            return True
        if channel != MessageChannel.Wechat.value:
            return False
        for config in ServiceConfigHelper.get_notification_configs():
            if config.name != self._source:
                continue
            return (config.config or {}).get("WECHAT_MODE", "app") != "bot"
        return False

    async def run(self, message: str, **kwargs) -> str:
        if not message:
            return "语音回复内容不能为空"

        voice_path = None
        used_voice = False
        channel = self._channel or ""
        if self._supports_real_voice_reply() and VoiceHelper.is_available("tts"):
            voice_file = await asyncio.to_thread(VoiceHelper.synthesize_speech, message)
            if voice_file:
                voice_path = str(voice_file)
                used_voice = True

        logger.info(
            "执行工具: %s, channel=%s, use_voice=%s, text_len=%s",
            self.name,
            channel,
            used_voice,
            len(message),
        )

        await ToolChain().async_post_message(
            Notification(
                channel=self._channel,
                source=self._source,
                mtype=NotificationType.Agent,
                userid=self._user_id,
                username=self._username,
                text=message,
                voice_path=voice_path,
                voice_caption=message if settings.AI_VOICE_REPLY_WITH_TEXT else None,
            )
        )
        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "voice" if used_voice else "text_fallback"

        if used_voice:
            return "语音回复已发送"
        return "当前未使用语音通道，已自动回退为文字回复"
