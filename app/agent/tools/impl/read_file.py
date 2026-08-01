"""文件读取工具"""

import hashlib
import json
from pathlib import Path
from typing import Optional, Type

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.log import logger

# 最大读取大小 50KB
MAX_READ_SIZE = 50 * 1024


class ReadFileInput(BaseModel):
    """文件读取工具的输入参数模型。"""

    file_path: str = Field(..., description="The absolute path of the file to read")
    start_line: Optional[int] = Field(None, description="The starting line number (1-based, inclusive). If not provided, reading starts from the beginning of the file.")
    end_line: Optional[int] = Field(None, description="The ending line number (1-based, inclusive). If not provided, reading goes until the end of the file.")
    include_metadata: bool = Field(
        False,
        description=(
            "Return structured JSON containing content, size, truncation state, "
            "and SHA-256. Use before a guarded full-file overwrite."
        ),
    )


class ReadFileTool(MoviePilotTool):
    """按行范围读取本地文本文件，并可返回文件版本元数据。"""

    name: str = "read_file"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.File,
    ]
    description: str = "Read the content of a text file. Supports reading by line range. Each read is limited to 50KB; content exceeding this limit will be truncated."
    args_schema: Type[BaseModel] = ReadFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"读取文件: {file_name}"

    async def run(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        include_metadata: bool = False,
        **kwargs,
    ) -> str:
        """读取指定文本范围，必要时附带完整文件的 SHA-256 元数据。"""
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}, start_line={start_line}, end_line={end_line}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="读取"
            )
            if access_error:
                return access_error

            path = AsyncPath(resolved_path)

            if not await path.exists():
                return f"错误：文件 {resolved_path} 不存在"

            if not await path.is_file():
                return f"错误：{resolved_path} 不是一个文件"

            raw_content = await path.read_bytes()
            content = raw_content.decode("utf-8", errors="replace")
            truncated = False

            if start_line is not None or end_line is not None:
                lines = content.splitlines(keepends=True)
                total_lines = len(lines)

                # 将行号转换为索引（1-based -> 0-based）
                s = (start_line - 1) if start_line and start_line >= 1 else 0
                e = end_line if end_line and end_line >= 1 else total_lines

                # 确保范围有效
                s = max(0, min(s, total_lines))
                e = max(s, min(e, total_lines))

                content = "".join(lines[s:e])

            # 检查大小限制
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_READ_SIZE:
                content = content_bytes[:MAX_READ_SIZE].decode("utf-8", errors="replace")
                truncated = True

            if include_metadata:
                return json.dumps(
                    {
                        "file_path": str(resolved_path),
                        "sha256": hashlib.sha256(raw_content).hexdigest(),
                        "size_bytes": len(raw_content),
                        "start_line": start_line,
                        "end_line": end_line,
                        "truncated": truncated,
                        "content": content,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            if truncated:
                return f"{content}\n\n[警告：文件内容已超过50KB限制，以上内容已被截断。请使用 start_line/end_line 参数分段读取。]"

            return content

        except PermissionError:
            return f"错误：没有权限读取 {file_path}"
        except UnicodeDecodeError:
            return f"错误：{file_path} 不是文本文件，无法读取"
        except Exception as e:
            logger.error(f"读取文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
