"""刮削媒体元数据工具"""

import asyncio
import json
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfoPath
from app.log import logger
from app.schemas import FileItem


class ScrapeMetadataInput(BaseModel):
    """刮削媒体元数据工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    path: str = Field(..., description="Path to the file or directory to scrape metadata for (e.g., '/path/to/file.mkv' or '/path/to/directory')")
    storage: Optional[str] = Field("local", description="Storage type: 'local' for local storage, 'smb', 'alist', etc. for remote storage (default: 'local')")
    overwrite: Optional[bool] = Field(False, description="Whether to overwrite existing metadata files (default: False)")


class ScrapeMetadataTool(MoviePilotTool):
    name: str = "scrape_metadata"
    description: str = "Scrape media metadata (NFO files, posters, backgrounds, etc.) for a file or directory. Automatically recognizes media information from the file path and generates metadata files. Supports both local and remote storage."
    args_schema: Type[BaseModel] = ScrapeMetadataInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据刮削参数生成友好的提示消息"""
        path = kwargs.get("path", "")
        storage = kwargs.get("storage", "local")
        overwrite = kwargs.get("overwrite", False)
        
        message = f"正在刮削媒体元数据: {path}"
        if storage != "local":
            message += f" [存储: {storage}]"
        if overwrite:
            message += " [覆盖模式]"
        
        return message

    async def run(self, path: str, storage: Optional[str] = "local",
                  overwrite: Optional[bool] = False, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: path={path}, storage={storage}, overwrite={overwrite}")
        
        try:
            # 验证路径
            if not path:
                return json.dumps({
                    "success": False,
                    "message": "刮削路径不能为空"
                }, ensure_ascii=False)
            
            # 创建 FileItem
            fileitem = FileItem(
                storage=storage,
                path=path,
                type="file" if Path(path).suffix else "dir"
            )
            
            # 检查本地存储路径是否存在
            if storage == "local":
                scrape_path = Path(path)
                if not scrape_path.exists():
                    return json.dumps({
                        "success": False,
                        "message": f"刮削路径不存在: {path}"
                    }, ensure_ascii=False)
            
            # 识别媒体信息
            media_chain = MediaChain()
            scrape_path = Path(path)
            meta = MetaInfoPath(scrape_path)
            mediainfo = await media_chain.async_recognize_by_meta(meta)
            
            if not mediainfo:
                return json.dumps({
                    "success": False,
                    "message": f"刮削失败，无法识别媒体信息: {path}",
                    "path": path
                }, ensure_ascii=False)
            
            # 在线程池中执行同步的刮削操作
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: media_chain.scrape_metadata(
                    fileitem=fileitem,
                    meta=meta,
                    mediainfo=mediainfo,
                    overwrite=overwrite
                )
            )
            
            return json.dumps({
                "success": True,
                "message": f"{path} 刮削完成",
                "path": path,
                "media_info": {
                    "title": mediainfo.title,
                    "year": mediainfo.year,
                    "type": mediainfo.type.value if mediainfo.type else None,
                    "tmdb_id": mediainfo.tmdb_id,
                    "season": mediainfo.season
                }
            }, ensure_ascii=False, indent=2)
        
        except Exception as e:
            error_message = f"刮削媒体元数据失败: {str(e)}"
            logger.error(f"刮削媒体元数据失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message,
                "path": path
            }, ensure_ascii=False)

