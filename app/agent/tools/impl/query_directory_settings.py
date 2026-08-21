"""查询系统目录设置工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.directory import DirectoryHelper
from app.runtime.log import logger
from app.schemas.file import FileURI


class QueryDirectorySettingsInput(BaseModel):
    """查询系统目录设置工具的输入参数模型"""
    directory_type: Optional[str] = Field("all",
                                          description="Filter directories by type: 'download' for download directories, 'library' for media library directories, 'all' for all directories")
    storage_type: Optional[str] = Field("all",
                                        description="Filter directories by storage type: 'local' for local storage, 'remote' for remote storage, 'all' for all storage types")
    name: Optional[str] = Field(None,
                               description="Filter directories by name (partial match, optional)")


class QueryDirectorySettingsTool(MoviePilotTool):
    name: str = "query_directory_settings"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Directory,
        ToolTag.Settings,
        ToolTag.Admin,
    ]
    description: str = "Query system directory configuration settings (NOT file listings). Returns configured directory paths, storage types, transfer modes, and other directory-related settings. Use 'list_directory' to list actual files and folders in a directory."
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryDirectorySettingsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        directory_type = kwargs.get("directory_type", "all")
        storage_type = kwargs.get("storage_type", "all")
        name = kwargs.get("name")
        
        parts = ["查询目录配置"]
        
        if directory_type != "all":
            type_map = {"download": "下载目录", "library": "媒体库目录"}
            parts.append(f"类型: {type_map.get(directory_type, directory_type)}")
        
        if storage_type != "all":
            storage_map = {"local": "本地存储", "remote": "远程存储"}
            parts.append(f"存储: {storage_map.get(storage_type, storage_type)}")
        
        if name:
            parts.append(f"名称: {name}")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    @staticmethod
    def _query_directory_settings(
        directory_type: Optional[str] = "all",
        storage_type: Optional[str] = "all",
        name: Optional[str] = None,
    ) -> str:
        """
        目录配置完全来自内存配置缓存，这里只做本地过滤和序列化。
        """
        directory_helper = DirectoryHelper()

        if directory_type == "download":
            dirs = directory_helper.get_download_dirs()
        elif directory_type == "library":
            dirs = directory_helper.get_library_dirs()
        else:
            dirs = directory_helper.get_dirs()

        filtered_dirs = []
        for d in dirs:
            if storage_type == "local":
                if directory_type == "download" and not FileURI.is_local(d.storage):
                    continue
                if directory_type == "library" and not FileURI.is_local(d.library_storage):
                    continue
                if directory_type == "all":
                    if d.download_path and not FileURI.is_local(d.storage):
                        continue
                    if d.library_path and not FileURI.is_local(d.library_storage):
                        continue
            elif storage_type == "remote":
                if directory_type == "download" and FileURI.is_local(d.storage):
                    continue
                if directory_type == "library" and FileURI.is_local(d.library_storage):
                    continue
                if directory_type == "all":
                    if d.download_path and FileURI.is_local(d.storage):
                        continue
                    if d.library_path and FileURI.is_local(d.library_storage):
                        continue

            if name and d.name and name.lower() not in d.name.lower():
                continue
            filtered_dirs.append(d)

        if not filtered_dirs:
            return "未找到相关目录配置"

        simplified_dirs = []
        for d in filtered_dirs:
            simplified_dirs.append(
                {
                    "name": d.name,
                    "priority": d.priority,
                    "storage": d.storage,
                    "download_path": d.download_path,
                    "library_path": d.library_path,
                    "library_storage": d.library_storage,
                    "media_type": d.media_type,
                    "media_category": d.media_category,
                    "monitor_type": d.monitor_type,
                    "monitor_mode": d.monitor_mode,
                    "transfer_type": d.transfer_type,
                    "overwrite_mode": d.overwrite_mode,
                    "renaming": d.renaming,
                    "scraping": d.scraping,
                    "notify": d.notify,
                    "download_type_folder": d.download_type_folder,
                    "download_category_folder": d.download_category_folder,
                    "library_type_folder": d.library_type_folder,
                    "library_category_folder": d.library_category_folder,
                }
            )

        return json.dumps(simplified_dirs, ensure_ascii=False, indent=2)

    async def run(self, directory_type: Optional[str] = "all",
                  storage_type: Optional[str] = "all",
                  name: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: directory_type={directory_type}, storage_type={storage_type}, name={name}")

        try:
            return self._query_directory_settings(
                directory_type=directory_type,
                storage_type=storage_type,
                name=name,
            )
        except Exception as e:
            logger.error(f"查询系统目录设置失败: {e}", exc_info=True)
            return f"查询系统目录设置时发生错误: {str(e)}"
