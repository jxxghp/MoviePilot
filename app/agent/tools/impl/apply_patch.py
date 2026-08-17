"""多文件补丁应用工具。

参考 Codex apply_patch 设计：一次调用可对多个文本文件执行新增、更新和删除，
先整体校验全部文件操作，通过后才逐个原子写盘。
"""

from dataclasses import dataclass, field
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
from app.runtime.log import logger

BEGIN_PATCH_MARKER = "*** Begin Patch"
END_PATCH_MARKER = "*** End Patch"
ADD_FILE_PREFIX = "*** Add File: "
UPDATE_FILE_PREFIX = "*** Update File: "
DELETE_FILE_PREFIX = "*** Delete File: "
HUNK_SEPARATOR_PREFIX = "@@"


class PatchParseError(ValueError):
    """补丁文本无法解析为合法的文件操作序列。"""


class PatchMatchError(ValueError):
    """补丁中的上下文或删除行与文件当前内容不一致。"""


@dataclass
class PatchHunk:
    """更新操作的单个替换片段：按序定位旧行并替换为新行。"""

    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    """补丁内针对单个文件的一次操作。"""

    operation: str  # "add" | "update" | "delete"
    path: str
    added_lines: list[str] = field(default_factory=list)
    hunks: list[PatchHunk] = field(default_factory=list)


def parse_patch(patch: str) -> list[FilePatch]:
    """解析以 Begin/End Patch 标记包裹的补丁文本为文件操作序列。"""
    lines = patch.strip().splitlines()
    if not lines or lines[0].strip() != BEGIN_PATCH_MARKER:
        raise PatchParseError(f"补丁必须以 '{BEGIN_PATCH_MARKER}' 开头")
    if len(lines) < 2 or lines[-1].strip() != END_PATCH_MARKER:
        raise PatchParseError(f"补丁必须以 '{END_PATCH_MARKER}' 结尾")

    operations: list[FilePatch] = []
    seen_paths: set[str] = set()
    current: Optional[FilePatch] = None
    current_hunk: Optional[PatchHunk] = None

    def close_hunk() -> None:
        """提交当前 hunk，无锚点的纯新增片段无法定位应直接报错。"""
        nonlocal current_hunk
        if current_hunk is None or current is None:
            return
        if not current_hunk.old_lines:
            raise PatchParseError(
                f"文件 {current.path} 的替换片段缺少上下文或删除行，"
                "无法定位替换位置，请在片段中包含至少一行不变的上下文"
            )
        current.hunks.append(current_hunk)
        current_hunk = None

    def open_section(operation: str, path: str) -> None:
        """开始新的文件段，同一文件在一次补丁中只允许出现一次。"""
        nonlocal current
        close_hunk()
        if not path:
            raise PatchParseError(f"'{operation}' 段落缺少文件路径")
        if path in seen_paths:
            raise PatchParseError(f"文件 {path} 在补丁中出现多次，请合并为一个段落")
        seen_paths.add(path)
        current = FilePatch(operation=operation, path=path)
        operations.append(current)

    for raw_line in lines[1:-1]:
        if raw_line.startswith(ADD_FILE_PREFIX):
            open_section("add", raw_line[len(ADD_FILE_PREFIX) :].strip())
        elif raw_line.startswith(UPDATE_FILE_PREFIX):
            open_section("update", raw_line[len(UPDATE_FILE_PREFIX) :].strip())
        elif raw_line.startswith(DELETE_FILE_PREFIX):
            open_section("delete", raw_line[len(DELETE_FILE_PREFIX) :].strip())
        elif current is None:
            raise PatchParseError(
                f"内容行出现在文件段落之前：{raw_line[:80]!r}，"
                "请先使用 '*** Add File:'、'*** Update File:' 或 '*** Delete File:' 声明文件"
            )
        elif current.operation == "add":
            if not raw_line.startswith("+"):
                raise PatchParseError(
                    f"文件 {current.path} 的新增段落只允许以 '+' 开头的行：{raw_line[:80]!r}"
                )
            current.added_lines.append(raw_line[1:])
        elif current.operation == "update":
            if raw_line.startswith(HUNK_SEPARATOR_PREFIX):
                close_hunk()
                current_hunk = PatchHunk()
            elif current_hunk is None:
                raise PatchParseError(
                    f"文件 {current.path} 的更新段落必须先出现 '@@' 片段分隔行"
                )
            elif raw_line.startswith("+"):
                current_hunk.new_lines.append(raw_line[1:])
            elif raw_line.startswith("-"):
                current_hunk.old_lines.append(raw_line[1:])
            else:
                # 上下文行遵循 diff 惯例，允许携带一个前导空格标记
                context_line = raw_line[1:] if raw_line.startswith(" ") else raw_line
                current_hunk.old_lines.append(context_line)
                current_hunk.new_lines.append(context_line)
        elif raw_line.strip():
            raise PatchParseError(
                f"文件 {current.path} 的删除段落不允许包含内容行：{raw_line[:80]!r}"
            )

    close_hunk()
    if not operations:
        raise PatchParseError("补丁不包含任何文件操作")
    return operations


def apply_hunks_to_content(
    content: str, hunks: list[PatchHunk], file_label: str
) -> str:
    """按顺序在文件内容中定位并替换每个 hunk，返回更新后的完整内容。"""
    ends_with_newline = content.endswith("\n")
    if ends_with_newline:
        content = content[:-1]

    offset = 0
    for index, hunk in enumerate(hunks, start=1):
        old_block = "\n".join(hunk.old_lines)
        new_block = "\n".join(hunk.new_lines)
        position = content.find(old_block, offset)
        if position < 0:
            raise PatchMatchError(
                f"文件 {file_label} 的第 {index} 个替换片段与当前内容不匹配，"
                "请重新读取文件并确认空格、缩进和换行"
            )
        content = (
            content[:position] + new_block + content[position + len(old_block) :]
        )
        offset = position + len(new_block)

    if ends_with_newline and content and not content.endswith("\n"):
        content += "\n"
    return content


def _delete_file(path: Path) -> None:
    """删除补丁中标记移除的已有文件。"""
    path.unlink()


class ApplyPatchInput(BaseModel):
    """补丁应用工具的输入参数模型。"""

    patch: str = Field(
        ...,
        description=(
            "A single patch wrapped in '*** Begin Patch' and '*** End Patch'. "
            "Sections: '*** Add File: <path>' whose body lines all start with "
            "'+'; '*** Update File: <path>' with hunks separated by '@@' lines, "
            "where '+' adds, '-' removes, and context lines may carry one "
            "leading space and must otherwise match the current file content "
            "exactly; '*** Delete File: <path>' with no body. Use one patch for "
            "all files touched by one logical change."
        ),
    )


class ApplyPatchTool(MoviePilotTool):
    """按补丁文本对多个本地文本文件执行新增、更新和删除。"""

    name: str = "apply_patch"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.File,
    ]
    description: str = (
        "Apply one unified patch to multiple local text files. Prefer it over "
        "edit_file when a single logical change spans several files, adds new "
        "files, or deletes files: submit one patch wrapped in '*** Begin Patch' "
        "/ '*** End Patch' with '*** Add File:', '*** Update File:', and "
        "'*** Delete File:' sections. Hunk context and removed lines must match "
        "the current file content exactly; the whole patch is validated before "
        "any file is written. For a single localized replacement in one "
        "already-read file, edit_file is simpler; use write_file to create one "
        "standalone new file. Non-admin users can only patch files inside the "
        "MoviePilot Agent config directory."
    )
    args_schema: Type[BaseModel] = ApplyPatchInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        patch = kwargs.get("patch", "") or ""
        file_count = sum(
            patch.count(prefix)
            for prefix in (ADD_FILE_PREFIX, UPDATE_FILE_PREFIX, DELETE_FILE_PREFIX)
        )
        return f"应用补丁: {file_count} 个文件" if file_count else "应用补丁"

    async def _plan_operations(
        self, operations: list[FilePatch]
    ) -> tuple[Optional[list], Optional[str]]:
        """校验每个文件操作并计算写入内容，全部通过才返回执行计划。"""
        planned = []
        for file_patch in operations:
            resolved_path, access_error = await self._check_local_file_access(
                file_patch.path, operation="打补丁"
            )
            if access_error:
                return None, access_error

            path = AsyncPath(resolved_path)
            exists = await path.exists()
            if file_patch.operation == "add":
                if exists:
                    return None, (
                        f"错误：文件 {resolved_path} 已存在，不能使用 Add File；"
                        "请改用 '*** Update File:' 或 edit_file 修改。"
                    )
                new_content = (
                    "\n".join(file_patch.added_lines) + "\n"
                    if file_patch.added_lines
                    else ""
                )
                planned.append((file_patch, resolved_path, Path(resolved_path), new_content, None))
                continue

            if not exists:
                if file_patch.operation == "update":
                    return None, (
                        f"错误：文件 {resolved_path} 不存在，不能使用 Update File；"
                        "请改用 '*** Add File:' 创建。"
                    )
                return None, f"错误：文件 {resolved_path} 不存在，无法删除"
            if not await path.is_file():
                return None, f"错误：{resolved_path} 不是一个文件"

            if file_patch.operation == "delete":
                planned.append((file_patch, resolved_path, Path(resolved_path), None, None))
                continue

            if not file_patch.hunks:
                return None, (
                    f"错误：文件 {resolved_path} 的 Update File 段落缺少 '@@' 替换片段"
                )
            local_path = Path(resolved_path)
            content = await path.read_text(encoding="utf-8", errors="strict")
            current_sha256 = await self.run_blocking(
                "default", calculate_file_sha256, local_path
            )
            new_content = apply_hunks_to_content(
                content, file_patch.hunks, resolved_path
            )
            planned.append(
                (file_patch, resolved_path, local_path, new_content, current_sha256)
            )
        return planned, None

    async def run(self, patch: str, **kwargs) -> str:
        """解析并整体校验补丁后，逐文件原子应用新增、更新和删除。"""
        logger.info("执行工具: apply_patch")

        try:
            try:
                operations = parse_patch(patch)
            except PatchParseError as error:
                return f"错误：{error}"

            planned, plan_error = await self._plan_operations(operations)
            if plan_error:
                return plan_error

            results = []
            for file_patch, resolved_path, local_path, new_content, sha256 in planned:
                if file_patch.operation == "delete":
                    await self.run_blocking("default", _delete_file, local_path)
                    results.append(f"删除 {resolved_path}")
                    continue
                if file_patch.operation == "add" and local_path.exists():
                    return (
                        f"错误：文件 {resolved_path} 在应用补丁期间被创建，拒绝覆盖。"
                        "请确认文件状态后重新应用补丁。"
                    )
                await self.run_blocking(
                    "default", atomic_write_text, local_path, new_content, sha256
                )
                new_sha256 = await self.run_blocking(
                    "default", calculate_file_sha256, local_path
                )
                verb = "新增" if file_patch.operation == "add" else "更新"
                results.append(f"{verb} {resolved_path}（sha256={new_sha256}）")

            logger.info(f"成功应用补丁，共处理 {len(results)} 个文件")
            return f"成功应用补丁（{len(results)} 个文件）：\n" + "\n".join(
                f"- {item}" for item in results
            )

        except PatchMatchError as error:
            return f"错误：{error}"
        except FileVersionConflictError:
            return (
                "错误：目标文件在应用补丁期间发生变化，拒绝写入。"
                "请重新读取文件并再次应用补丁。"
            )
        except FileNotFoundError:
            return (
                "错误：目标文件在应用补丁期间被删除，拒绝继续。"
                "请确认文件状态后重新应用补丁。"
            )
        except PermissionError:
            return "错误：没有访问/修改补丁目标文件的权限"
        except UnicodeDecodeError:
            return "错误：补丁目标文件不是文本文件，无法应用补丁"
        except Exception as e:
            logger.error(f"应用补丁时发生错误: {str(e)}", exc_info=True)
            return f"操作失败: {str(e)}"
