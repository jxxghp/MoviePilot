"""识别媒体信息工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import media_type_to_agent


class RecognizeMediaInput(BaseModel):
    """识别媒体信息工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    title: Optional[str] = Field(None, description="The title of the torrent/media to recognize (required for torrent recognition)")
    subtitle: Optional[str] = Field(None, description="The subtitle or description of the torrent (optional, helps improve recognition accuracy)")
    path: Optional[str] = Field(None, description="The file path to recognize (required for file recognition, mutually exclusive with title)")


class RecognizeMediaTool(MoviePilotTool):
    name: str = "recognize_media"
    description: str = "Extract/identify media information from torrent titles or file paths (NOT database search). Supports two modes: 1) Extract from torrent title and optional subtitle, 2) Extract from file path. Returns detailed media information. Use 'search_media' to search TMDB database, or 'scrape_metadata' to generate metadata files for existing files."
    args_schema: Type[BaseModel] = RecognizeMediaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据识别参数生成友好的提示消息"""
        title = kwargs.get("title")
        subtitle = kwargs.get("subtitle")
        path = kwargs.get("path")
        
        if path:
            message = f"识别文件媒体信息: {path}"
        elif title:
            message = f"识别种子媒体信息: {title}"
            if subtitle:
                message += f" ({subtitle})"
        else:
            message = "识别媒体信息"
        
        return message

    async def run(self, title: Optional[str] = None, subtitle: Optional[str] = None,
                  path: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: title={title}, subtitle={subtitle}, path={path}")
        
        try:
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
            result["media_info"] = {
                "title": media_info.get("title"),
                "en_title": media_info.get("en_title"),
                "year": media_info.get("year"),
                "type": media_type_to_agent(media_info.get("type")),
                "season": media_info.get("season"),
                "tmdb_id": media_info.get("tmdb_id"),
                "imdb_id": media_info.get("imdb_id"),
                "douban_id": media_info.get("douban_id"),
                "bangumi_id": media_info.get("bangumi_id"),
                "overview": media_info.get("overview"),
                "vote_average": media_info.get("vote_average"),
                "poster_path": media_info.get("poster_path"),
                "backdrop_path": media_info.get("backdrop_path"),
                "detail_link": media_info.get("detail_link"),
                "title_year": media_info.get("title_year"),
                "source": media_info.get("source")
            }
        
        # 处理元数据信息
        if meta_info:
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
                "tmdbid": meta_info.get("tmdbid"),
                "doubanid": meta_info.get("doubanid")
            }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
