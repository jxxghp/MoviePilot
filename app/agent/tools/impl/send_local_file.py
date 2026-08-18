"""发送本地附件工具。"""

from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.message import MessageType
from app.schemas.notification import ChannelCapabilityManager, ChannelCapability, channel_identity, resolve_channel


class SendLocalFileInput(BaseModel):
    """发送本地附件工具输入。"""

    file_path: str = Field(
        ...,
        description="Absolute path to the local image or file to send to the user",
    )
    message: Optional[str] = Field(
        None,
        description="Optional message or caption to send with the attachment",
    )
    title: Optional[str] = Field(
        None,
        description="Optional short title shown together with the attachment",
    )
    file_name: Optional[str] = Field(
        None,
        description="Optional override filename presented to the user when downloading",
    )

    @model_validator(mode="after")
    def validate_file_path(self):
        if not self.file_path:
            raise ValueError("file_path 不能为空")
        return self


class SendLocalFileTool(MoviePilotTool):
    name: str = "send_local_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Message,
        ToolTag.File,
    ]
    sends_message: bool = True
    description: str = (
        "Send a local image or file from the server filesystem to the current user. "
        "Use this when you have generated or identified a local file the user should download."
    )
    args_schema: Type[BaseModel] = SendLocalFileInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"发送本地附件: {file_name}"

    async def run(
        self,
        file_path: str,
        message: Optional[str] = None,
        title: Optional[str] = None,
        file_name: Optional[str] = None,
        **kwargs,
    ) -> str:
        if not self._channel or not self._source:
            return "当前不在可回传消息的会话中，无法发送附件"

        channel = resolve_channel(self._channel)
        if not channel:
            return f"不支持的消息渠道: {self._channel}"

        if not ChannelCapabilityManager.supports_capability(
            channel, ChannelCapability.FILE_SENDING
        ):
            return f"当前渠道 {channel_identity(channel)} 暂不支持发送本地文件"

        resolved_path = Path(file_path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = resolved_path.resolve()
        if not resolved_path.exists() or not resolved_path.is_file():
            return f"文件不存在: {resolved_path}"

        logger.info(
            "执行工具: %s, channel=%s, file=%s",
            self.name,
            channel_identity(channel),
            resolved_path,
        )

        await self.send_message(
            Message(
                channel=channel,
                source=self._source,
                mtype=MessageType.Agent,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message,
                file_path=str(resolved_path),
                file_name=file_name or resolved_path.name,
                save_history=False,
            )
        )
        return "本地附件已发送"
