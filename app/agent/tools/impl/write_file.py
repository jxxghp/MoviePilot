"""文件写入工具"""

from pathlib import Path
from typing import Optional, Type

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._file_write_utils import (
    FileVersionConflictError,
    atomic_write_text,
    calculate_file_sha256,
)
from app.agent.tools.tags import ToolTag
from app.log import logger


class WriteFileInput(BaseModel):
    """文件写入工具的输入参数模型。"""

    file_path: str = Field(..., description="The absolute path of the file to write")
    content: str = Field(..., description="The content to write into the file")
    overwrite: bool = Field(
        False,
        description=(
            "Allow replacing an existing file in full. Keep false when creating a "
            "new file; prefer edit_file for localized changes."
        ),
    )
    expected_sha256: Optional[str] = Field(
        None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description=(
            "Optional SHA-256 returned by read_file(include_metadata=true). When "
            "overwriting, fail if the existing file no longer has this hash."
        ),
    )


class WriteFileTool(MoviePilotTool):
    """创建本地文本文件，或在显式允许后完整覆盖已有文件。"""

    name: str = "write_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.File,
    ]
    description: str = (
        "Create a local text file with complete content. Existing files are "
        "protected unless overwrite=true; localized changes should use edit_file. "
        "Supports an optional SHA-256 conflict check and writes atomically. "
        "Non-admin users can only write inside the MoviePilot Agent config directory."
    )
    args_schema: Type[BaseModel] = WriteFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"写入文件: {file_name}"

    async def run(
        self,
        file_path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: Optional[str] = None,
        **kwargs,
    ) -> str:
        """创建或显式覆盖文件，并通过可选哈希阻止陈旧写入。"""
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="写入"
            )
            if access_error:
                return access_error

            path = AsyncPath(resolved_path)

            exists = await path.exists()
            if exists and not await path.is_file():
                return f"错误：{resolved_path} 路径已存在但不是一个文件"
            if exists and not overwrite:
                return (
                    f"错误：文件 {resolved_path} 已存在，拒绝完整覆盖。"
                    "局部修改请使用 edit_file；确需重写时设置 overwrite=true。"
                )
            if expected_sha256 and not exists:
                return (
                    f"错误：文件 {resolved_path} 不存在，无法校验 expected_sha256。"
                    "请确认路径和最新文件状态。"
                )

            local_path = Path(resolved_path)
            current_sha256 = None
            if exists:
                current_sha256 = await self.run_blocking(
                    "default", calculate_file_sha256, local_path
                )
            if expected_sha256:
                if current_sha256.casefold() != expected_sha256.casefold():
                    return (
                        f"错误：文件 {resolved_path} 已在读取后发生变化，拒绝覆盖。"
                        "请重新读取文件并基于最新内容写入。"
                    )

            await self.run_blocking(
                "default",
                atomic_write_text,
                local_path,
                content,
                current_sha256,
            )
            new_sha256 = await self.run_blocking(
                "default", calculate_file_sha256, local_path
            )

            logger.info(f"成功写入文件 {resolved_path}")
            return f"成功写入文件 {resolved_path}（sha256={new_sha256}）"

        except FileVersionConflictError:
            return (
                f"错误：文件 {file_path} 在写入期间发生变化，拒绝覆盖。"
                "请重新读取文件并再次写入。"
            )
        except PermissionError:
            return f"错误：没有权限写入 {file_path}"
        except Exception as e:
            logger.error(f"写入文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
