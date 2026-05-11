"""搜索媒体工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.log import logger
from app.schemas.types import MediaType, media_type_to_agent


class SearchMediaInput(BaseModel):
    """搜索媒体工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    title: str = Field(..., description="The title of the media to search for (e.g., 'The Matrix', 'Breaking Bad')")
    year: Optional[str] = Field(None, description="Release year of the media (optional, helps narrow down results)")
    media_type: Optional[str] = Field(None,
                                      description="Allowed values: movie, tv")
    season: Optional[int] = Field(None,
                                  description="Season number for TV shows and anime (optional, only applicable for series)")


class SearchMediaTool(MoviePilotTool):
    name: str = "search_media"
    description: str = "Search TMDB database for media resources (movies, TV shows, anime, etc.) by title, year, type, and other criteria. Returns detailed media information from TMDB. Use 'recognize_media' to extract info from torrent titles/file paths, or 'scrape_metadata' to generate metadata files."
    args_schema: Type[BaseModel] = SearchMediaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        title = kwargs.get("title", "")
        year = kwargs.get("year")
        media_type = kwargs.get("media_type")
        season = kwargs.get("season")
        
        message = f"搜索媒体: {title}"
        if year:
            message += f" ({year})"
        if media_type:
            message += f" [{media_type}]"
        if season:
            message += f" 第{season}季"
        
        return message

    async def run(self, title: str, year: Optional[str] = None,
                  media_type: Optional[str] = None, season: Optional[int] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, year={year}, media_type={media_type}, season={season}")

        try:
            media_chain = MediaChain()
            # 使用 MediaChain.search 方法
            meta, results = await media_chain.async_search(title=title)

            # 过滤结果
            if results:
                media_type_enum = None
                if media_type:
                    media_type_enum = MediaType.from_agent(media_type)
                    if not media_type_enum:
                        return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"

                filtered_results = []
                for result in results:
                    if year and result.year != year:
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
                        simplified = {
                            "title": r.title,
                            "en_title": r.en_title,
                            "year": r.year,
                            "type": media_type_to_agent(r.type),
                            "season": r.season,
                            "tmdb_id": r.tmdb_id,
                            "imdb_id": r.imdb_id,
                            "douban_id": r.douban_id,
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
