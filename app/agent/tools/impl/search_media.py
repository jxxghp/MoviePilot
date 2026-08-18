"""搜索媒体工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.orchestration.media import MediaChain
from app.runtime.log import logger
from app.schemas.types import MediaType, media_type_to_agent
from app.schemas.media import resolve_media_identity
from app.domain.media import normalize_music_type
from ._music_utils import simplify_music_info


class SearchMediaInput(BaseModel):
    """搜索媒体工具的输入参数模型"""
    title: str = Field(..., description="The title of the media to search for (e.g., 'The Matrix', 'Breaking Bad')")
    year: Optional[str] = Field(None, description="Release year of the media (optional, helps narrow down results)")
    media_type: Optional[str] = Field(None,
                                      description="Allowed values: movie, tv, music")
    music_type: Optional[str] = Field(
        None,
        description="Music entity filter: recording, album, or artist. Only valid when media_type='music'",
    )
    season: Optional[int] = Field(None,
                                  description="Season number for TV shows and anime (optional, only applicable for series)")


class SearchMediaTool(MoviePilotTool):
    """按标题搜索影视或音乐元数据候选。"""

    name: str = "search_media"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
    ]
    description: str = (
        "Search metadata databases for movies, TV shows, music recordings, albums, or artists. "
        "For music, set media_type='music' and optionally filter music_type as recording, album, or artist. "
        "Returns source-native IDs that must be reused for detail, subscription, torrent, and library operations."
    )
    args_schema: Type[BaseModel] = SearchMediaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        title = kwargs.get("title", "")
        year = kwargs.get("year")
        media_type = kwargs.get("media_type")
        music_type = kwargs.get("music_type")
        season = kwargs.get("season")
        
        message = f"搜索媒体: {title}"
        if year:
            message += f" ({year})"
        if media_type:
            message += f" [{media_type}]"
        if music_type:
            message += f" [{music_type}]"
        if season is not None:
            message += f" 第{season}季"
        
        return message

    async def run(self, title: str, year: Optional[str] = None,
                  media_type: Optional[str] = None, season: Optional[int] = None,
                  music_type: Optional[str] = None, **kwargs) -> str:
        """执行元数据搜索并返回可复用的精简媒体身份。"""
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, year={year}, "
            f"media_type={media_type}, season={season}, music_type={music_type}")

        try:
            media_type_enum = None
            if media_type:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'music'"

            if music_type and media_type_enum != MediaType.MUSIC:
                return "错误：music_type 仅能与 media_type='music' 一起使用"

            if media_type_enum == MediaType.MUSIC:
                if season is not None:
                    return "错误：音乐没有季号，搜索音乐时不能传入 season"
                normalized_music_type = None
                if music_type:
                    normalized_music_type = normalize_music_type(music_type)
                    if not normalized_music_type:
                        return (
                            f"错误：无效的音乐实体类型 '{music_type}'，"
                            "支持的类型：'recording', 'album', 'artist'"
                        )
                results = await MediaChain().async_search_music(query=title, limit=100)
                filtered_music = [
                    item
                    for item in results or []
                    if (not year or str(item.year or "") == str(year))
                    and (
                        not normalized_music_type
                        or item.music_type == normalized_music_type
                    )
                ]
                if not filtered_music:
                    return f"未找到符合条件的音乐资源: {title}"
                total_count = len(filtered_music)
                limited_results = filtered_music[:30]
                result_json = json.dumps(
                    [simplify_music_info(item) for item in limited_results],
                    ensure_ascii=False,
                    indent=2,
                )
                if total_count > len(limited_results):
                    return (
                        f"注意：搜索结果共找到 {total_count} 条，为节省上下文空间，"
                        f"仅显示前 {len(limited_results)} 条结果。\n\n{result_json}"
                    )
                return result_json

            media_chain = MediaChain()
            _, results = await media_chain.async_search(title=title)

            # 过滤结果
            if results:
                filtered_results = []
                for result in results:
                    if year and str(result.year or "") != str(year):
                        continue
                    if media_type_enum and result.type != media_type_enum:
                        continue
                    if season is not None and result.season != season:
                        continue
                    filtered_results.append(result)

                if filtered_results:
                    # 搜索结果只返回前 30 条，后续可通过更精确的年份/类型条件缩小范围。
                    total_count = len(filtered_results)
                    limited_results = filtered_results[:30]
                    # 精简字段，只保留关键信息
                    simplified_results = []
                    for r in limited_results:
                        media_source, media_id = resolve_media_identity(media=r)
                        simplified = {
                            "title": r.title,
                            "en_title": r.en_title,
                            "year": r.year,
                            "type": media_type_to_agent(r.type),
                            "season": r.season,
                            "tmdb_id": r.tmdb_id,
                            "imdb_id": r.imdb_id,
                            "douban_id": r.douban_id,
                            "bangumi_id": r.bangumi_id,
                            "anilist_id": r.anilist_id,
                            "media_source": media_source,
                            "media_id": media_id,
                            "overview": r.overview[:200] + "..." if r.overview and len(r.overview) > 200 else r.overview,
                            "vote_average": r.vote_average,
                            "poster_path": r.poster_path,
                            "detail_link": r.detail_link
                        }
                        simplified_results.append(simplified)
                    result_json = json.dumps(simplified_results, ensure_ascii=False, indent=2)
                    # 如果结果被裁剪，添加提示信息
                    if total_count > len(limited_results):
                        return f"注意：搜索结果共找到 {total_count} 条，为节省上下文空间，仅显示前 {len(limited_results)} 条结果。\n\n{result_json}"
                    return result_json
                else:
                    return f"未找到符合条件的媒体资源: {title}"
            else:
                return f"未找到相关媒体资源: {title}"
        except Exception as e:
            error_message = f"搜索媒体失败: {str(e)}"
            logger.error(f"搜索媒体失败: {e}", exc_info=True)
            return error_message
