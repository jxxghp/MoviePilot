"""
发送本地附件工具。
"""

from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageType, NotificationChannel


class SendLocalFileInput(BaseModel):
    """
    发送本地附件工具输入。
    """

    file_path: str = Field(
        ...,
        description="发送给当前用户的本地图片或文件绝对路径",
    )
    message: Optional[str] = Field(
        None,
        description="随附件发送的可选说明或文字",
    )
    title: Optional[str] = Field(
        None,
        description="随附件展示的可选简短标题",
    )
    file_name: Optional[str] = Field(
        None,
        description="用户下载时展示的可选替代文件名",
    )

    @model_validator(mode="after")
    def validate_file_path(self) -> "SendLocalFileInput":
        """
        拒绝空的附件路径。

        :return: 通过检查的当前输入模型
        :raises ValueError: file_path 为空
        """
        if not self.file_path:
            raise ValueError("file_path 不能为空")
        return self


class SendLocalFileTool(MoviePilotTool):
    """
    在支持附件的渠道中向当前用户发送本地文件。
    """
    name: str = Field(
        default="send_local_file",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Write, ToolTag.Message, ToolTag.File],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    sends_message: bool = Field(
        default=True,
        description="工具是否直接向当前用户发送消息",
    )
    description: str = Field(
        default="将服务器文件系统中的本地图片或文件发送给当前用户。已生成或找到需要让用户下载的本地文件时使用。",
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=SendLocalFileInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        根据本地附件路径生成发送进度提示。

        :param kwargs: 工具输入参数，使用其中的 file_path
        :return: 包含附件文件名的用户提示
        """
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"发送本地附件: {file_name}"

    async def run(  # type: ignore[override]
        self,
        file_path: str,
        message: Optional[str] = None,
        title: Optional[str] = None,
        file_name: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        通过当前会话渠道发送已有本地附件。

        :param file_path: 附件路径，展开用户目录并将相对路径解析为绝对路径
        :param message: 随附件发送的可选正文
        :param title: 随附件展示的可选标题
        :param file_name: 下载展示名称；未提供时使用实际文件名
        :param kwargs: 工具调用框架传入的扩展参数
        :return: 发送成功提示；渠道不支持、会话缺失或文件无效时返回原因
        """
        if not self._channel or not self._source:
            return "当前不在可回传消息的会话中，无法发送附件"

        try:
            channel = NotificationChannel(self._channel)
        except ValueError:
            return f"不支持的消息渠道: {self._channel}"

        if not ChannelCapabilityManager.supports_capability(
            channel, ChannelCapability.FILE_SENDING
        ):
            return f"当前渠道 {channel.value} 暂不支持发送本地文件"

        resolved_path = Path(file_path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = resolved_path.resolve()
        if not resolved_path.exists() or not resolved_path.is_file():
            return f"文件不存在: {resolved_path}"

        logger.info(
            "执行工具: %s, channel=%s, file=%s",
            self.name,
            channel.value,
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
