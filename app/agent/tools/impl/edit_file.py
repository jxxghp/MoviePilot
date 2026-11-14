"""
文件精确编辑工具。
"""

import json
from pathlib import Path
from typing import Any, Optional, Type

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._file_write_utils import (
    FileVersionConflictError,
    atomic_write_text,
    calculate_file_sha256,
)
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class EditFileInput(BaseModel):
    """
    文件编辑工具的输入参数模型。
    """

    file_path: str = Field(
        ...,
        description="待编辑文件的绝对路径",
    )
    old_text: str = Field(
        ...,
        description="需要精确替换的非空旧文本；除非 replace_all=True，否则必须唯一定位一处",
    )
    new_text: str = Field(
        ...,
        description="用于替换的新文本；空字符串表示删除",
    )
    replace_all: bool = Field(
        False,
        description="是否替换全部精确匹配，默认 False，使有歧义的普通代码编辑失败而非改动多处",
    )
    expected_sha256: Optional[str] = Field(
        None,
        pattern="^[0-9a-fA-F]{64}$",
        description="read_file(include_metadata=true) 返回的可选 SHA-256；文件自读取后变化时拒绝修改",
    )


class EditFileTool(MoviePilotTool):
    """
    使用精确文本匹配安全编辑本地文件。
    """

    name: str = Field(
        default="edit_file",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Write, ToolTag.File],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "精确匹配旧文本并编辑已有本地文本文件。"
            "默认要求只匹配一处，仅在有意批量替换时使用 replace_all。"
            "old_text 不得为空，新文件须用 write_file 创建。"
            "支持可选 SHA-256 冲突检查。"
            "同一逻辑变更跨多个文件或需要新增、删除文件时使用 apply_patch。"
            "非管理员只能编辑 MoviePilot Agent 配置目录内的文件。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=EditFileInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        根据参数生成友好的提示消息
        """
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"编辑文件: {file_name}"

    async def run(  # type: ignore[override]
        self,
        file_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        expected_sha256: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        校验精确匹配和可选文件版本后，以原子方式写入编辑结果。
        """
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="编辑"
            )
            if access_error:
                return access_error
            if resolved_path is None:
                return "错误：无法解析编辑路径"

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
            new_sha256 = await self.run_blocking(
                "default",
                atomic_write_text,
                local_path,
                new_content,
                current_sha256,
            )

            logger.info(
                f"成功编辑文件 {resolved_path}，替换了 {replacement_count} 处内容"
            )
            return (
                f"成功编辑文件 {resolved_path}（替换了 {replacement_count} 处匹配内容，"
                f"sha256={new_sha256}）"
            )

        except FileVersionConflictError:
            current_sha256 = None
            try:
                current_sha256 = await self.run_blocking(
                    "default", calculate_file_sha256, Path(file_path)
                )
            except (OSError, ValueError):
                pass
            return json.dumps({
                "success": False,
                "execution_outcome": "failed",
                "error": f"文件 {file_path} 在编辑期间发生变化，拒绝覆盖。",
                "current_sha256": current_sha256,
                "recovery": "重新读取文件并取得新的 sha256，再基于最新内容编辑；不要覆盖未知版本。",
            }, ensure_ascii=False)
        except PermissionError:
            return f"错误：没有访问/修改 {file_path} 的权限"
        except UnicodeDecodeError:
            return f"错误：{file_path} 不是文本文件，无法编辑"
        except Exception as e:
            logger.error(f"编辑文件 {file_path} 时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
