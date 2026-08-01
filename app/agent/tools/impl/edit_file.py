"""文件精确编辑工具。"""

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


class EditFileInput(BaseModel):
    """文件编辑工具的输入参数模型。"""

    file_path: str = Field(..., description="The absolute path of the file to edit")
    old_text: str = Field(
        ...,
        description=(
            "The exact old text to replace. It must be non-empty and uniquely "
            "identify one location unless replace_all is true."
        ),
    )
    new_text: str = Field(..., description="The new text to replace with")
    replace_all: bool = Field(
        False,
        description=(
            "Replace every exact match. Keep false for normal code edits so an "
            "ambiguous match fails instead of changing multiple locations."
        ),
    )
    expected_sha256: Optional[str] = Field(
        None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description=(
            "Optional SHA-256 returned by read_file(include_metadata=true). The "
            "edit fails if the file changed after it was read."
        ),
    )


class EditFileTool(MoviePilotTool):
    """使用精确文本匹配安全编辑本地文件。"""

    name: str = "edit_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.File,
    ]
    description: str = (
        "Edit an existing local text file using an exact text match. By default "
        "the match must occur exactly once; use replace_all only for intentional "
        "bulk replacement. old_text cannot be empty, and new files must be "
        "created with write_file. Supports an optional SHA-256 conflict check. "
        "Non-admin users can only edit files inside the MoviePilot Agent config "
        "directory."
    )
    args_schema: Type[BaseModel] = EditFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"编辑文件: {file_name}"

    async def run(
        self,
        file_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        expected_sha256: Optional[str] = None,
        **kwargs,
    ) -> str:
        """校验精确匹配和可选文件版本后，以原子方式写入编辑结果。"""
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="编辑"
            )
            if access_error:
                return access_error

            if not old_text:
                return "错误：old_text 不能为空；创建或完整写入文件请使用 write_file。"

            path = AsyncPath(resolved_path)
            if not await path.exists():
                return f"错误：文件 {resolved_path} 不存在；创建文件请使用 write_file。"

            if not await path.is_file():
                return f"错误：{resolved_path} 不是一个文件"

            local_path = Path(resolved_path)
            current_sha256 = await self.run_blocking(
                "default", calculate_file_sha256, local_path
            )
            if (
                expected_sha256
                and current_sha256.casefold() != expected_sha256.casefold()
            ):
                return (
                    f"错误：文件 {resolved_path} 已在读取后发生变化，拒绝覆盖。"
                    "请重新读取文件并基于最新内容编辑。"
                )

            content = await path.read_text(encoding="utf-8", errors="strict")
            occurrences = content.count(old_text)
            if occurrences == 0:
                logger.warning(f"编辑文件 {resolved_path} 失败：未找到指定的旧文本块")
                return (
                    f"错误：在文件 {resolved_path} 中未找到指定的旧文本。"
                    "请重新读取文件并确认空格、缩进和换行。"
                )
            if occurrences > 1 and not replace_all:
                return (
                    f"错误：old_text 在文件 {resolved_path} 中匹配到 {occurrences} 处，"
                    "为避免误改已拒绝编辑。请提供更多上下文使其唯一，或明确设置 "
                    "replace_all=true。"
                )

            replacement_count = occurrences if replace_all else 1
            new_content = content.replace(
                old_text,
                new_text,
                -1 if replace_all else 1,
            )
            await self.run_blocking(
                "default",
                atomic_write_text,
                local_path,
                new_content,
                current_sha256,
            )
            new_sha256 = await self.run_blocking(
                "default", calculate_file_sha256, local_path
            )

            logger.info(
                f"成功编辑文件 {resolved_path}，替换了 {replacement_count} 处内容"
            )
            return (
                f"成功编辑文件 {resolved_path}（替换了 {replacement_count} 处匹配内容，"
                f"sha256={new_sha256}）"
            )

        except FileVersionConflictError:
            return (
                f"错误：文件 {file_path} 在编辑期间发生变化，拒绝覆盖。"
                "请重新读取文件并再次编辑。"
            )
        except PermissionError:
            return f"错误：没有访问/修改 {file_path} 的权限"
        except UnicodeDecodeError:
            return f"错误：{file_path} 不是文本文件，无法编辑"
        except Exception as e:
            logger.error(f"编辑文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
