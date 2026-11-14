"""
文件写入工具
"""

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


class WriteFileInput(BaseModel):
    """
    文件写入工具的输入参数模型。
    """

    file_path: str = Field(
        ...,
        description="待写入文本文件的绝对路径",
    )
    content: str = Field(
        ...,
        description="写入文件的完整文本内容",
    )
    overwrite: bool = Field(
        False,
        description="是否允许完整覆盖已有文件，默认 False；局部修改优先使用 edit_file",
    )
    expected_sha256: Optional[str] = Field(
        None,
        pattern="^[0-9a-fA-F]{64}$",
        description="read_file(include_metadata=true) 返回的可选 SHA-256；覆盖时若现有文件哈希变化则拒绝写入",
    )


class WriteFileTool(MoviePilotTool):
    """
    创建本地文本文件，或在显式允许后完整覆盖已有文件。
    """

    name: str = Field(
        default="write_file",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Write, ToolTag.File],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "以完整内容创建本地文本文件。"
            "已有文件默认受保护，只有 overwrite=true 才可覆盖；"
            "局部变更应使用 edit_file。"
            "支持可选 SHA-256 冲突检查，并采用原子写入。"
            "非管理员只能写入 MoviePilot Agent 配置目录。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=WriteFileInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        根据参数生成友好的提示消息
        """
        file_path = kwargs.get("file_path", "")
        file_name = Path(file_path).name if file_path else "未知文件"
        return f"写入文件: {file_name}"

    async def run(  # type: ignore[override]
        self,
        file_path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        创建或显式覆盖文件，并通过可选哈希阻止陈旧写入。
        """
        logger.info(f"执行工具: {self.name}, 参数: file_path={file_path}")

        try:
            resolved_path, access_error = await self._check_local_file_access(
                file_path, operation="写入"
            )
            if access_error:
                return access_error
            if resolved_path is None:
                return "错误：无法解析写入路径"

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
                if current_sha256 is None or current_sha256.casefold() != expected_sha256.casefold():
                    return (
                        f"错误：文件 {resolved_path} 已在读取后发生变化，拒绝覆盖。"
                        "请重新读取文件并基于最新内容写入。"
                    )

            new_sha256 = await self.run_blocking(
                "default",
                atomic_write_text,
                local_path,
                content,
                current_sha256,
                overwrite=overwrite,
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
