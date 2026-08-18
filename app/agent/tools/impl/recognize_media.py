"""识别媒体信息工具"""

import json
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.orchestration.media import MediaChain
from app.runtime.config import settings
from app.domain.context import Context
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.types import MediaType, media_type_to_agent
from ._music_utils import simplify_music_info


class RecognizeMediaInput(BaseModel):
    """识别媒体信息工具的输入参数模型"""
    title: Optional[str] = Field(None, description="The title of the torrent/media to recognize (required for torrent recognition)")
    subtitle: Optional[str] = Field(None, description="The subtitle or description of the torrent (optional, helps improve recognition accuracy)")
    path: Optional[str] = Field(None, description="The file path to recognize (required for file recognition, mutually exclusive with title)")
    media_type: Optional[str] = Field(
        None,
        description="Allowed values: movie, tv, music. Audio file paths are automatically treated as music",
    )
    artist: Optional[str] = Field(
        None,
        description="Artist name for music title recognition. Only valid with media_type='music'",
    )
    album: Optional[str] = Field(
        None,
        description="Album name for music title recognition. Only valid with media_type='music'",
    )


class RecognizeMediaTool(MoviePilotTool):
    """从标题或文件路径识别影视与单曲信息。"""

    name: str = "recognize_media"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
        ToolTag.Metadata,
    ]
    description: str = (
        "Extract media information from torrent titles or file paths. Supports movies, TV, and individual "
        "audio files. For music title recognition set media_type='music' and provide artist when known. "
        "Album and artist browsing by stable ID belongs to query_media_detail; use scrape_metadata for directories."
    )
    args_schema: Type[BaseModel] = RecognizeMediaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据识别参数生成友好的提示消息"""
        title = kwargs.get("title")
        subtitle = kwargs.get("subtitle")
        path = kwargs.get("path")
        media_type = kwargs.get("media_type")
        
        if path:
            message = f"识别文件媒体信息: {path}"
        elif title:
            message = f"识别种子媒体信息: {title}"
            if subtitle:
                message += f" ({subtitle})"
        else:
            message = "识别媒体信息"
        if media_type:
            message += f" [{media_type}]"
        
        return message

    async def run(self, title: Optional[str] = None, subtitle: Optional[str] = None,
                  path: Optional[str] = None, media_type: Optional[str] = None,
                  artist: Optional[str] = None, album: Optional[str] = None,
                  **kwargs) -> str:
        """执行路径或标题识别，并按媒体类型选择正确的元数据链。"""
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, subtitle={subtitle}, "
            f"path={path}, media_type={media_type}, artist={artist}, album={album}"
        )
        
        try:
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
            if (artist or album) and media_type_enum != MediaType.MUSIC:
                return json.dumps({
                    "success": False,
                    "message": "artist 和 album 仅能与 media_type='music' 一起使用",
                }, ensure_ascii=False)

            is_audio_path = bool(
                path and Path(path).suffix.lower() in settings.RMT_AUDIOEXT
            )
            recognize_music = media_type_enum == MediaType.MUSIC or (
                media_type_enum is None and is_audio_path
            )
            if recognize_music:
                if path:
                    if not is_audio_path:
                        return json.dumps({
                            "success": False,
                            "message": (
                                "音乐路径识别只支持单个音频文件；目录请使用 "
                                "scrape_metadata(media_type='music')"
                            ),
                            "path": path,
                        }, ensure_ascii=False)
                    # 影视与音乐共用统一路径识别入口，音频后缀自动路由到音乐识别链
                    context = await MediaChain().async_recognize_by_path(path)
                    if context:
                        return self._format_context_result(context, "音频文件")
                    return json.dumps({
                        "success": False,
                        "message": f"无法识别音乐信息: {path}",
                        "path": path,
                    }, ensure_ascii=False)
                if title:
                    metainfo = MetaMusic.parse_query(title)
                    if artist:
                        metainfo.artists = [artist]
                    if album:
                        metainfo.album = album
                    mediainfo = await MediaChain().async_recognize_by_meta(metainfo)
                    if mediainfo:
                        context = Context(meta_info=metainfo, media_info=mediainfo)
                        return self._format_context_result(context, "音乐标题")
                    return json.dumps({
                        "success": False,
                        "message": f"无法识别音乐信息: {title}",
                        "title": title,
                        "artist": artist,
                        "album": album,
                    }, ensure_ascii=False)
                return json.dumps({
                    "success": False,
                    "message": "音乐识别必须提供 title 或单个音频文件 path",
                }, ensure_ascii=False)

            media_chain = MediaChain()

            # 根据提供的参数选择识别方式
            if path:
                # 文件路径识别
                if not path:
                    return json.dumps({
                        "success": False,
                        "message": "文件路径不能为空"
                    }, ensure_ascii=False)
                
                context = await media_chain.async_recognize_by_path(
                    path,
                    obtain_images=False,
                )
                if context:
                    return self._format_context_result(context, "文件")
                else:
                    return json.dumps({
                        "success": False,
                        "message": f"无法识别文件媒体信息: {path}",
                        "path": path
                    }, ensure_ascii=False)
            
            elif title:
                # 种子标题识别
                metainfo = MetaInfo(title, subtitle)
                mediainfo = await media_chain.async_recognize_by_meta(
                    metainfo,
                    obtain_images=False,
                )
                if mediainfo:
                    context = Context(meta_info=metainfo, media_info=mediainfo)
                    return self._format_context_result(context, "种子")
                else:
                    return json.dumps({
                        "success": False,
                        "message": f"无法识别种子媒体信息: {title}",
                        "title": title,
                        "subtitle": subtitle
                    }, ensure_ascii=False)
            
            else:
                return json.dumps({
                    "success": False,
                    "message": "必须提供 title（标题）或 path（文件路径）参数之一"
                }, ensure_ascii=False)
        
        except Exception as e:
            error_message = f"识别媒体信息失败: {str(e)}"
            logger.error(f"识别媒体信息失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message
            }, ensure_ascii=False)

    @staticmethod
    def _format_context_result(context: Context, source_type: str) -> str:
        """格式化识别结果为JSON字符串"""
        if not context:
            return json.dumps({
                "success": False,
                "message": "识别结果为空"
            }, ensure_ascii=False)
        
        context_dict = context.to_dict()
        media_info = context_dict.get("media_info")
        meta_info = context_dict.get("meta_info")
        
        # 构建简化的结果
        result = {
            "success": True,
            "source_type": source_type,
            "media_info": None,
            "meta_info": None
        }
        
        # 处理媒体信息
        if media_info:
            if getattr(context.media_info, "type", None) == MediaType.MUSIC:
                result["media_info"] = simplify_music_info(context.media_info)
            else:
                result["media_info"] = {
                    "title": media_info.get("title"),
                    "en_title": media_info.get("en_title"),
                    "year": media_info.get("year"),
                    "type": media_type_to_agent(media_info.get("type")),
                    "season": media_info.get("season"),
                    "media_source": media_info.get("media_source"),
                    "media_id": media_info.get("media_id"),
                    "overview": media_info.get("overview"),
                    "vote_average": media_info.get("vote_average"),
                    "poster_path": media_info.get("poster_path"),
                    "backdrop_path": media_info.get("backdrop_path"),
                    "detail_link": media_info.get("detail_link"),
                    "title_year": media_info.get("title_year"),
                }
        
        # 处理元数据信息
        if meta_info:
            if getattr(context.media_info, "type", None) == MediaType.MUSIC:
                result["meta_info"] = {
                    key: meta_info.get(key)
                    for key in (
                        "title", "artists", "album", "album_artist", "year",
                        "disc_number", "track_number", "total_discs", "total_tracks",
                        "version", "audio_format", "bit_depth", "sample_rate", "bitrate",
                        "duration", "isrc", "media_source", "media_id",
                    )
                    if meta_info.get(key) not in (None, "", [])
                }
            else:
                result["meta_info"] = {
                    "name": meta_info.get("name"),
                    "title": meta_info.get("title"),
                    "year": meta_info.get("year"),
                    "type": media_type_to_agent(meta_info.get("type")),
                    "begin_season": meta_info.get("begin_season"),
                    "end_season": meta_info.get("end_season"),
                    "begin_episode": meta_info.get("begin_episode"),
                    "end_episode": meta_info.get("end_episode"),
                    "total_episode": meta_info.get("total_episode"),
                    "part": meta_info.get("part"),
                    "season_episode": meta_info.get("season_episode"),
                    "episode_list": meta_info.get("episode_list"),
                    "media_source": meta_info.get("media_source"),
                    "media_id": meta_info.get("media_id"),
                }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
