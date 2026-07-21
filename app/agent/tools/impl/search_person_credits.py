"""搜索演员参演作品工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.chain.bangumi import BangumiChain
from app.log import logger
from app.utils.media import resolve_media_identity


class SearchPersonCreditsInput(BaseModel):
    """搜索演员参演作品工具的输入参数模型"""
    person_id: int = Field(..., description="The ID of the person/actor to search for credits (e.g., 31 for Tom Hanks in TMDB)")
    source: str = Field(..., description="The data source: 'tmdb' for TheMovieDB, 'douban' for Douban, 'bangumi' for Bangumi")
    page: Optional[int] = Field(1, description="Page number for pagination (default: 1)")


class SearchPersonCreditsTool(MoviePilotTool):
    name: str = "search_person_credits"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
    ]
    description: str = "Search for films and TV shows that a person/actor has appeared in (filmography). Supports searching by person ID from TMDB, Douban, or Bangumi database. Returns a list of media works the person has participated in."
    args_schema: Type[BaseModel] = SearchPersonCreditsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        person_id = kwargs.get("person_id", "")
        source = kwargs.get("source", "")
        return f"搜索人物参演作品: {source} ID {person_id}"

    async def run(self, person_id: int, source: str, page: Optional[int] = 1, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: person_id={person_id}, source={source}, page={page}")

        try:
            # 根据source选择相应的chain
            if source.lower() == "tmdb":
                tmdb_chain = TmdbChain()
                medias = await tmdb_chain.async_person_credits(person_id=person_id, page=page)
            elif source.lower() == "douban":
                douban_chain = DoubanChain()
                medias = await douban_chain.async_person_credits(person_id=person_id, page=page)
            elif source.lower() == "bangumi":
                bangumi_chain = BangumiChain()
                medias = await bangumi_chain.async_person_credits(person_id=person_id)
            else:
                return f"不支持的数据源: {source}。支持的数据源: tmdb, douban, bangumi"

            if medias:
                # 限制最多30条结果
                total_count = len(medias)
                limited_medias = medias[:30]
                # 精简字段，只保留关键信息
                simplified_results = []
                for media in limited_medias:
                    media_source, media_id = resolve_media_identity(media=media)
                    simplified = {
                        "title": media.title,
                        "en_title": media.en_title,
                        "year": media.year,
                        "type": media.type.value if media.type else None,
                        "season": media.season,
                        "tmdb_id": media.tmdb_id,
                        "imdb_id": media.imdb_id,
                        "douban_id": media.douban_id,
                        "bangumi_id": media.bangumi_id,
                        "anilist_id": media.anilist_id,
                        "media_source": media_source,
                        "media_id": media_id,
                        "overview": media.overview[:200] + "..." if media.overview and len(media.overview) > 200 else media.overview,
                        "vote_average": media.vote_average,
                        "poster_path": media.poster_path,
                        "backdrop_path": media.backdrop_path,
                        "detail_link": media.detail_link
                    }
                    simplified_results.append(simplified)
                
                result_json = json.dumps(simplified_results, ensure_ascii=False, indent=2)
                # 如果结果被裁剪，添加提示信息
                if total_count > 30:
                    return f"注意：搜索结果共找到 {total_count} 条，为节省上下文空间，仅显示前 30 条结果。\n\n{result_json}"
                return result_json
            else:
                return f"未找到人物 ID {person_id} ({source}) 的参演作品"
        except Exception as e:
            error_message = f"搜索演员参演作品失败: {str(e)}"
            logger.error(f"搜索演员参演作品失败: {e}", exc_info=True)
            return error_message
