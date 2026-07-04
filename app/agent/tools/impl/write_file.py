"""文件写入工具"""

from pathlib import Path
from typing import Optional, Type

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.log import logger


class WriteFileInput(BaseModel):
    """文件写入工具的输入参数模型。"""

    file_path: str = Field(..., description="The absolute path of the file to write")
    content: str = Field(..., description="The content to write into the file")


class WriteFileTool(MoviePilotTool):
    name: str = "write_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.File,
    ]
    description: str = (
        "Write full content to a local text file. Non-admin users can only write "
        "inside the MoviePilot Agent config directory."
    )
    args_schema: Type[BaseModel] = WriteFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"写入文件: {file_name}"

    async def run(self, file_path: str, content: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="写入"
            )
            if access_error:
                return access_error

            path = AsyncPath(resolved_path)

            if await path.exists() and not await path.is_file():
                return f"错误：{resolved_path} 路径已存在但不是一个文件"

            # 自动创建父目录
            await path.parent.mkdir(parents=True, exist_ok=True)

            # 写入文件
            await path.write_text(content, encoding="utf-8")

            logger.info(f"成功写入文件 {resolved_path}")
            return f"成功写入文件 {resolved_path}"

        except PermissionError:
            return f"错误：没有权限写入 {file_path}"
        except Exception as e:
            logger.error(f"写入文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
