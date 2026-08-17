"""刮削媒体元数据工具"""

import json
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.runtime.config import settings
from app.runtime.log import logger
from app.schemas.workflow import FileItem
from app.schemas.types import (
    MUSIC_ENTITY_ARTIST,
    MediaSource,
    MediaType,
    media_type_to_agent,
)
from app.schemas.media import normalize_media_source
from app.domain.media import normalize_music_type
from ._music_utils import simplify_music_info


class ScrapeMetadataInput(BaseModel):
    """刮削媒体元数据工具的输入参数模型"""

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
    media_type: Optional[str] = Field(
        None,
        description="Allowed values: movie, tv, music. Audio files are auto-detected; music directories should set music explicitly",
    )
    music_type: Optional[str] = Field(
        None,
        description="For an explicit music ID: recording for one file or album for a complete album directory",
    )
    media_source: Optional[MediaSource] = Field(
        None,
        description=(
            "Music metadata source: musicbrainz, theaudiodb, or doubanmusic. "
            "When omitted, automatic music recognition compares all sources. "
            "Must be paired with media_id when an ID is supplied"
        ),
    )
    media_id: Optional[str] = Field(
        None,
        description="Source-native recording or album ID. Must be paired with media_source",
    )


class ScrapeMetadataTool(MoviePilotTool):
    """刮削影视 NFO/图片或音乐标签、封面与旁挂歌词。"""

    name: str = "scrape_metadata"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Media,
        ToolTag.Metadata,
        ToolTag.File,
        ToolTag.Admin,
    ]
    description: str = (
        "Scrape existing movie, TV, or music files on local/remote storage. Video generates configured NFO and "
        "images. Music applies configured audio-tag and cover policies and can automatically download LRCLIB "
        "lyrics as same-name .lrc/.txt sidecars. A directory with an album ID is treated as one complete album; "
        "without an ID, each audio file is recognized independently."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = ScrapeMetadataInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据刮削参数生成友好的提示消息"""
        path = kwargs.get("path", "")
        storage = kwargs.get("storage", "local")
        overwrite = kwargs.get("overwrite", False)
        media_type = kwargs.get("media_type")

        message = f"刮削媒体元数据: {path}"
        if storage != "local":
            message += f" [存储: {storage}]"
        if overwrite:
            message += " [覆盖模式]"
        if media_type:
            message += f" [{media_type}]"

        return message

    async def run(
        self,
        path: str,
        storage: Optional[str] = "local",
        overwrite: Optional[bool] = False,
        media_type: Optional[str] = None,
        music_type: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> str:
        """识别刮削类型并将同步文件及外部元数据操作放入线程池。"""
        logger.info(
            f"执行工具: {self.name}, 参数: path={path}, storage={storage}, "
            f"overwrite={overwrite}, media_type={media_type}, music_type={music_type}, "
            f"media_source={media_source}, media_id={media_id}"
        )

        try:
            # 验证路径
            if not path:
                return json.dumps(
                    {"success": False, "message": "刮削路径不能为空"},
                    ensure_ascii=False,
                )

            media_type_enum = None
            if media_type:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return json.dumps({
                        "success": False,
                        "message": (
                            f"无效的媒体类型 '{media_type}'，"
                            "支持的类型：'movie', 'tv', 'music'"
                        ),
                    }, ensure_ascii=False)
            explicit_identity = media_source is not None or media_id is not None
            normalized_source = normalize_media_source(media_source)
            normalized_media_id = str(media_id).strip() if media_id is not None else ""
            if explicit_identity and (
                    not normalized_source or not normalized_media_id
            ):
                return json.dumps({
                    "success": False,
                    "message": "必须同时提供有效的 media_source 和 media_id",
                }, ensure_ascii=False)
            media_source = normalized_source
            media_id = normalized_media_id or None

            local_path = Path(path)
            is_local_directory = (storage or "local") == "local" and local_path.is_dir()
            file_type = "dir" if is_local_directory or not local_path.suffix else "file"
            fileitem = FileItem(
                storage=storage or "local",
                path=path,
                type=file_type,
            )

            # 检查本地存储路径是否存在
            if storage == "local":
                if not Path(path).exists():
                    return json.dumps(
                        {"success": False, "message": f"刮削路径不存在: {path}"},
                        ensure_ascii=False,
                    )

            media_chain = MediaChain()
            scraping_chain = ScrapingChain()
            is_audio_file = (
                fileitem.type == "file"
                and Path(path).suffix.lower() in settings.RMT_AUDIOEXT
            )
            scrape_music = media_type_enum == MediaType.MUSIC or (
                media_type_enum is None and is_audio_file
            )
            if scrape_music:
                normalized_music_type = None
                if music_type:
                    normalized_music_type = normalize_music_type(music_type)
                    if not normalized_music_type:
                        return json.dumps({
                            "success": False,
                            "message": (
                                f"无效的音乐实体类型 '{music_type}'，"
                                "支持的类型：'recording', 'album'"
                            ),
                        }, ensure_ascii=False)
                    if normalized_music_type == MUSIC_ENTITY_ARTIST:
                        return json.dumps({
                            "success": False,
                            "message": "艺术家是浏览实体，不能用于文件刮削",
                        }, ensure_ascii=False)

                mediainfo = None
                if media_source and media_id:
                    recognize_kwargs = {
                        "media_source": media_source,
                        "media_id": media_id,
                        "mtype": MediaType.MUSIC,
                    }
                    if normalized_music_type:
                        recognize_kwargs["music_type"] = normalized_music_type
                    mediainfo = await media_chain.async_recognize_media(
                        **recognize_kwargs
                    )
                    if not mediainfo:
                        return json.dumps({
                            "success": False,
                            "message": f"未识别到音乐信息: {media_source}:{media_id}",
                        }, ensure_ascii=False)
                    actual_music_type = getattr(mediainfo, "music_type", None)
                    if normalized_music_type and actual_music_type != normalized_music_type:
                        return json.dumps({
                            "success": False,
                            "message": (
                                f"音乐实体类型不匹配：请求 {normalized_music_type}，"
                                f"实际 {actual_music_type or 'unknown'}"
                            ),
                        }, ensure_ascii=False)

                success, message = await self.run_blocking(
                    "storage",
                    scraping_chain.scrape_music_metadata,
                    fileitem=fileitem,
                    mediainfo=mediainfo,
                    overwrite=bool(overwrite),
                    media_source=media_source,
                )
                result = {
                    "success": success,
                    "message": message,
                    "path": path,
                    "type": "music",
                }
                if mediainfo:
                    result["media_info"] = simplify_music_info(mediainfo)
                return json.dumps(result, ensure_ascii=False, indent=2)

            if music_type or media_source or media_id:
                return json.dumps({
                    "success": False,
                    "message": "music_type、media_source 和 media_id 仅能用于音乐刮削",
                }, ensure_ascii=False)

            # 影视沿用路径识别与 NFO/图片刮削链路。
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
                scraping_chain.scrape_metadata,
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
                        "type": media_type_to_agent(context.media_info.type),
                        "media_source": context.media_info.media_source,
                        "media_id": context.media_info.media_id,
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
