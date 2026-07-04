"""文件编辑工具"""

from pathlib import Path
from typing import Optional, Type

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.log import logger


class EditFileInput(BaseModel):
    """文件编辑工具的输入参数模型。"""

    file_path: str = Field(..., description="The absolute path of the file to edit")
    old_text: str = Field(..., description="The exact old text to be replaced")
    new_text: str = Field(..., description="The new text to replace with")


class EditFileTool(MoviePilotTool):
    name: str = "edit_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.File,
    ]
    description: str = (
        "Edit a local text file by replacing specific old text with new text. "
        "Non-admin users can only edit files inside the MoviePilot Agent config "
        "directory."
    )
    args_schema: Type[BaseModel] = EditFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"编辑文件: {file_name}"

    async def run(self, file_path: str, old_text: str, new_text: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="编辑"
            )
            if access_error:
                return access_error

            path = AsyncPath(resolved_path)
            # 校验逻辑：如果要替换特定文本，文件必须存在且包含该文本
            if not await path.exists():
                # 如果 old_text 为空，可能用户想直接创建文件，但通常 edit_file 需要匹配旧内容
                if old_text:
                    return f"错误：文件 {resolved_path} 不存在，无法进行内容替换。"

            if await path.exists() and not await path.is_file():
                return f"错误：{resolved_path} 不是一个文件"

            if await path.exists():
                content = await path.read_text(encoding="utf-8", errors="replace")
                if old_text not in content:
                    logger.warning(f"编辑文件 {resolved_path} 失败：未找到指定的旧文本块")
                    return f"错误：在文件 {resolved_path} 中未找到指定的旧文本。请确保包含所有的空格、缩进 and 换行符。"
                occurrences = content.count(old_text)
                new_content = content.replace(old_text, new_text)
            else:
                # 文件不存在且 old_text 为空的情形（初始化新文件）
                new_content = new_text
                occurrences = 1

            # 自动创建父目录
            await path.parent.mkdir(parents=True, exist_ok=True)

            # 写入文件
            await path.write_text(new_content, encoding="utf-8")

            logger.info(f"成功编辑文件 {resolved_path}，替换了 {occurrences} 处内容")
            return f"成功编辑文件 {resolved_path} (替换了 {occurrences} 处匹配内容)"

        except PermissionError:
            return f"错误：没有访问/修改 {file_path} 的权限"
        except UnicodeDecodeError:
            return f"错误：{file_path} 不是文本文件，无法编辑"
        except Exception as e:
            logger.error(f"编辑文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
