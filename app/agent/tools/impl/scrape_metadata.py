"""刮削媒体元数据工具"""

import json
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.log import logger
from app.schemas import FileItem


class ScrapeMetadataInput(BaseModel):
    """刮削媒体元数据工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    path: str = Field(
        ...,
        description="Path to the file or directory to scrape metadata for (e.g., '/path/to/file.mkv' or '/path/to/directory')",
    )
    storage: Optional[str] = Field(
        "local",
        description="Storage type: 'local' for local storage, 'smb', 'alist', etc. for remote storage (default: 'local')",
    )
    overwrite: Optional[bool] = Field(
        False,
        description="Whether to overwrite existing metadata files (default: False)",
    )


class ScrapeMetadataTool(MoviePilotTool):
    name: str = "scrape_metadata"
    description: str = "Generate metadata files (NFO files, posters, backgrounds, etc.) for existing media files or directories. Automatically recognizes media information from the file path and creates metadata files. Supports both local and remote storage. Use 'search_media' to search TMDB database, or 'recognize_media' to extract info from torrent titles/file paths without generating files."
    require_admin: bool = True
    args_schema: Type[BaseModel] = ScrapeMetadataInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据刮削参数生成友好的提示消息"""
        path = kwargs.get("path", "")
        storage = kwargs.get("storage", "local")
        overwrite = kwargs.get("overwrite", False)

        message = f"刮削媒体元数据: {path}"
        if storage != "local":
            message += f" [存储: {storage}]"
        if overwrite:
            message += " [覆盖模式]"

        return message

    async def run(
        self,
        path: str,
        storage: Optional[str] = "local",
        overwrite: Optional[bool] = False,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: path={path}, storage={storage}, overwrite={overwrite}"
        )

        try:
            # 验证路径
            if not path:
                return json.dumps(
                    {"success": False, "message": "刮削路径不能为空"},
                    ensure_ascii=False,
                )

            # 创建 FileItem
            fileitem = FileItem(
                storage=storage, path=path, type="file" if Path(path).suffix else "dir"
            )

            # 检查本地存储路径是否存在
            if storage == "local":
                if not Path(path).exists():
                    return json.dumps(
                        {"success": False, "message": f"刮削路径不存在: {path}"},
                        ensure_ascii=False,
                    )

            # 识别媒体信息
            media_chain = MediaChain()
            context = await media_chain.async_recognize_by_path(
                path,
                obtain_images=True,
            )

            if not context or not context.media_info:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"刮削失败，无法识别媒体信息: {path}",
                        "path": path,
                    },
                    ensure_ascii=False,
                )

            # 刮削会包含磁盘写入和外部图片/元数据访问，统一放到 storage 线程池。
            await self.run_blocking(
                "storage",
                media_chain.scrape_metadata,
                fileitem=fileitem,
                meta=context.meta_info,
                mediainfo=context.media_info,
                overwrite=overwrite,
            )

            return json.dumps(
                {
                    "success": True,
                    "message": f"{path} 刮削完成",
                    "path": path,
                    "media_info": {
                        "title": context.media_info.title,
                        "year": context.media_info.year,
                        "type": context.media_info.type.value if context.media_info.type else None,
                        "tmdb_id": context.media_info.tmdb_id,
                        "season": context.media_info.season,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )

        except Exception as e:
            error_message = f"刮削媒体元数据失败: {str(e)}"
            logger.error(f"刮削媒体元数据失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": error_message, "path": path},
                ensure_ascii=False,
            )
