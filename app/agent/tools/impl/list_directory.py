"""查询文件系统目录内容工具"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.storage import StorageChain
from app.log import logger
from app.schemas.file import FileItem
from app.utils.string import StringUtils


DEFAULT_DIRECTORY_PAGE_SIZE = 50
MAX_DIRECTORY_PAGE_SIZE = 200


class ListDirectoryInput(BaseModel):
    """查询文件系统目录内容工具的输入参数模型"""
    path: str = Field(..., description="Directory path to list contents (e.g., '/home/user/downloads' or 'C:/Downloads')")
    storage: Optional[str] = Field("local", description="Storage type (default: 'local' for local file system, can be 'smb', 'alist', etc.)")
    sort_by: Optional[str] = Field("name", description="Sort order: 'name' for alphabetical sorting, 'time' for modification time sorting (default: 'name')")
    limit: Optional[int] = Field(
        DEFAULT_DIRECTORY_PAGE_SIZE,
        ge=1,
        le=MAX_DIRECTORY_PAGE_SIZE,
        description=(
            f"Maximum items to return in this page (default: {DEFAULT_DIRECTORY_PAGE_SIZE}, "
            f"maximum: {MAX_DIRECTORY_PAGE_SIZE})"
        ),
    )
    offset: Optional[int] = Field(
        0,
        ge=0,
        description="Number of sorted directory items to skip before this page",
    )


class ListDirectoryTool(MoviePilotTool):
    """分页查询本地或远程存储目录中的文件和子目录。"""

    name: str = "list_directory"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Directory,
        ToolTag.File,
    ]
    description: str = (
        "List actual files and folders in a file system directory (NOT configuration). "
        "Shows files and subdirectories with their names, types, sizes, and modification "
        f"times. Returns a page of up to {DEFAULT_DIRECTORY_PAGE_SIZE} items with total "
        f"count and next offset; limit is capped at {MAX_DIRECTORY_PAGE_SIZE}. "
        "Use 'query_directory_settings' to query directory configuration settings."
    )
    args_schema: Type[BaseModel] = ListDirectoryInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据目录参数生成友好的提示消息"""
        path = kwargs.get("path", "")
        storage = kwargs.get("storage", "local")
        
        message = f"查询目录: {path}"
        if storage != "local":
            message += f" [存储: {storage}]"
        
        return message

    @staticmethod
    def _list_directory_sync(
        path: str,
        storage: Optional[str] = "local",
        sort_by: Optional[str] = "name",
        limit: Optional[int] = DEFAULT_DIRECTORY_PAGE_SIZE,
        offset: Optional[int] = 0,
    ) -> str:
        """
        目录遍历可能触发本地磁盘或远程存储请求，统一放到线程池中执行并分页返回。
        """
        if not path:
            return "错误：路径不能为空"

        if storage == "local":
            if not path.startswith("/") and not (len(path) > 1 and path[1] == ":"):
                path = str(Path(path).resolve())
        elif not path.startswith("/"):
            path = "/" + path

        fileitem = FileItem(storage=storage or "local", path=path, type="dir")
        file_list = StorageChain().list_files(fileitem, recursion=False)

        if file_list is None:
            return f"无法访问目录：{path}，请检查路径是否正确或存储是否可用"
        if sort_by == "time":
            file_list.sort(key=lambda x: x.modify_time or 0, reverse=True)
        else:
            file_list.sort(
                key=lambda x: (
                    0 if x.type == "dir" else 1,
                    StringUtils.natural_sort_key(x.name or ""),
                )
            )

        total_count = len(file_list)
        normalized_limit = max(
            1,
            min(int(limit or DEFAULT_DIRECTORY_PAGE_SIZE), MAX_DIRECTORY_PAGE_SIZE),
        )
        normalized_offset = max(0, int(offset or 0))
        limited_list = file_list[
            normalized_offset : normalized_offset + normalized_limit
        ]
        simplified_items = []
        for item in limited_list:
            size_str = StringUtils.str_filesize(item.size) if item.size else None
            modify_time_str = None
            if item.modify_time:
                try:
                    modify_time_str = datetime.fromtimestamp(item.modify_time).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, OSError):
                    modify_time_str = str(item.modify_time)

            simplified = {
                "name": item.name,
                "type": item.type,
                "path": item.path,
                "size": size_str,
                "modify_time": modify_time_str,
            }
            if item.type == "file" and item.extension:
                simplified["extension"] = item.extension
            simplified_items.append(simplified)

        returned_count = len(simplified_items)
        has_more = normalized_offset + returned_count < total_count
        return json.dumps(
            {
                "items": simplified_items,
                "total_count": total_count,
                "returned_count": returned_count,
                "limit": normalized_limit,
                "offset": normalized_offset,
                "has_more": has_more,
                "next_offset": (
                    normalized_offset + returned_count if has_more else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    async def run(self, path: str, storage: Optional[str] = "local",
                  sort_by: Optional[str] = "name",
                  limit: Optional[int] = DEFAULT_DIRECTORY_PAGE_SIZE,
                  offset: Optional[int] = 0,
                  **kwargs) -> str:
        """
        分页查询指定目录的文件和子目录。

        :param path: 要查询的目录路径
        :param storage: 存储类型，默认为本地存储
        :param sort_by: 排序方式，支持名称或修改时间
        :param limit: 当前页最大条数，最高不超过工具上限
        :param offset: 当前页起始偏移量
        :return: 包含项目列表和分页元数据的 JSON 字符串
        """
        logger.info(f"执行工具: {self.name}, 参数: path={path}, storage={storage}, sort_by={sort_by}")

        try:
            resolved_path, access_error = await self._check_local_storage_access(
                path=path, storage=storage, operation="列出"
            )
            if access_error:
                return access_error
            if resolved_path:
                path = str(resolved_path)
            return await self.run_blocking(
                "storage",
                self._list_directory_sync,
                path,
                storage,
                sort_by,
                limit,
                offset,
            )
        except Exception as e:
            logger.error(f"查询目录内容失败: {e}", exc_info=True)
            return f"查询目录内容时发生错误: {str(e)}"
