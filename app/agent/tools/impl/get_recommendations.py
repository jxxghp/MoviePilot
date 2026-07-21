"""获取推荐工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.recommend import RecommendChain
from app.log import logger
from app.schemas.types import MediaType, media_type_to_agent


class GetRecommendationsInput(BaseModel):
    """获取推荐工具的输入参数模型"""

    source: Optional[str] = Field(
        "tmdb_trending",
        description="Recommendation source: "
        "'tmdb_trending' for TMDB trending content, "
        "'tmdb_movies' for TMDB popular movies, "
        "'tmdb_tvs' for TMDB popular TV shows, "
        "'douban_hot' for Douban popular content, "
        "'douban_movie_hot' for Douban hot movies, "
        "'douban_tv_hot' for Douban hot TV shows, "
        "'douban_movie_showing' for Douban movies currently showing, "
        "'douban_movies' for Douban latest movies, "
        "'douban_tvs' for Douban latest TV shows, "
        "'douban_movie_top250' for Douban movie TOP250, "
        "'douban_tv_weekly_chinese' for Douban Chinese TV weekly chart, "
        "'douban_tv_weekly_global' for Douban global TV weekly chart, "
        "'douban_tv_animation' for Douban popular animation, "
        "'bangumi_calendar' for Bangumi anime calendar",
    )
    media_type: Optional[str] = Field(
        "all", description="Allowed values: movie, tv, all"
    )
    page: Optional[int] = Field(
        1, description="Page number for pagination (default: 1, 20 items per page)"
    )


class GetRecommendationsTool(MoviePilotTool):
    name: str = "get_recommendations"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
        ToolTag.Recommendation,
    ]
    description: str = "Get trending and popular media recommendations from various sources. Returns curated lists of popular movies, TV shows, and anime based on different criteria like trending, ratings, or calendar schedules. Supports pagination with 20 items per page."
    args_schema: Type[BaseModel] = GetRecommendationsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据推荐参数生成友好的提示消息"""
        source = kwargs.get("source", "tmdb_trending")
        media_type = kwargs.get("media_type", "all")
        page = kwargs.get("page", 1)

        source_map = {
            "tmdb_trending": "TMDB流行趋势",
            "tmdb_movies": "TMDB热门电影",
            "tmdb_tvs": "TMDB热门电视剧",
            "douban_hot": "豆瓣热门",
            "douban_movie_hot": "豆瓣热门电影",
            "douban_tv_hot": "豆瓣热门电视剧",
            "douban_movie_showing": "豆瓣热映",
            "douban_movies": "豆瓣最新电影",
            "douban_tvs": "豆瓣最新电视剧",
            "douban_movie_top250": "豆瓣电影TOP250",
            "douban_tv_weekly_chinese": "豆瓣国产剧集榜",
            "douban_tv_weekly_global": "豆瓣全球剧集榜",
            "douban_tv_animation": "豆瓣热门动漫",
            "bangumi_calendar": "番组计划",
        }
        source_desc = source_map.get(source, source)

        message = f"获取推荐: {source_desc}"
        if media_type != "all":
            message += f" [{media_type}]"
        message += f" (第{page}页)"

        return message

    async def run(
        self,
        source: Optional[str] = "tmdb_trending",
        media_type: Optional[str] = "all",
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        page = max(1, page or 1)
        page_size = 20
        logger.info(
            f"执行工具: {self.name}, 参数: source={source}, media_type={media_type}, page={page}"
        )
        try:
            if media_type != "all":
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'all'"
                media_type = media_type_enum.to_agent()  # 归一化为 "movie"/"tv"

            recommend_chain = RecommendChain()
            results = []
            if source == "tmdb_trending":
                results = await recommend_chain.async_tmdb_trending(page=page)
            elif source == "tmdb_movies":
                results = await recommend_chain.async_tmdb_movies(page=page)
            elif source == "tmdb_tvs":
                results = await recommend_chain.async_tmdb_tvs(page=page)
            elif source == "douban_hot":
                if media_type == "movie":
                    results = await recommend_chain.async_douban_movie_hot(
                        page=page, count=page_size
                    )
                elif media_type == "tv":
                    results = await recommend_chain.async_douban_tv_hot(
                        page=page, count=page_size
                    )
                else:  # all
                    results.extend(
                        await recommend_chain.async_douban_movie_hot(
                            page=page, count=page_size
                        )
                    )
                    results.extend(
                        await recommend_chain.async_douban_tv_hot(
                            page=page, count=page_size
                        )
                    )
            elif source == "douban_movie_hot":
                results = await recommend_chain.async_douban_movie_hot(
                    page=page, count=page_size
                )
            elif source == "douban_tv_hot":
                results = await recommend_chain.async_douban_tv_hot(
                    page=page, count=page_size
                )
            elif source == "douban_movie_showing":
                results = await recommend_chain.async_douban_movie_showing(
                    page=page, count=page_size
                )
            elif source == "douban_movies":
                results = await recommend_chain.async_douban_movies(
                    page=page, count=page_size
                )
            elif source == "douban_tvs":
                results = await recommend_chain.async_douban_tvs(
                    page=page, count=page_size
                )
            elif source == "douban_movie_top250":
                results = await recommend_chain.async_douban_movie_top250(
                    page=page, count=page_size
                )
            elif source == "douban_tv_weekly_chinese":
                results = await recommend_chain.async_douban_tv_weekly_chinese(
                    page=page, count=page_size
                )
            elif source == "douban_tv_weekly_global":
                results = await recommend_chain.async_douban_tv_weekly_global(
                    page=page, count=page_size
                )
            elif source == "douban_tv_animation":
                results = await recommend_chain.async_douban_tv_animation(
                    page=page, count=page_size
                )
            elif source == "bangumi_calendar":
                results = await recommend_chain.async_bangumi_calendar(
                    page=page, count=page_size
                )
            else:
                # 不支持的推荐来源
                supported_sources = [
                    "tmdb_trending",
                    "tmdb_movies",
                    "tmdb_tvs",
                    "douban_hot",
                    "douban_movie_hot",
                    "douban_tv_hot",
                    "douban_movie_showing",
                    "douban_movies",
                    "douban_tvs",
                    "douban_movie_top250",
                    "douban_tv_weekly_chinese",
                    "douban_tv_weekly_global",
                    "douban_tv_animation",
                    "bangumi_calendar",
                ]
                return f"不支持的推荐来源: {source}。支持的来源包括: {', '.join(supported_sources)}"

            if results:
                # 对于TMDB来源，API自身按页返回，取前page_size条
                total_count = len(results)
                page_results = results[:page_size]
                # 精简字段，只保留关键信息
                simplified_results = []
                for r in page_results:
                    # r 应该是字典格式（to_dict的结果），但为了安全起见进行检查
                    if not isinstance(r, dict):
                        logger.warning(f"推荐结果格式异常，跳过: {type(r)}")
                        continue

                    simplified = {
                        "title": r.get("title"),
                        "en_title": r.get("en_title"),
                        "year": r.get("year"),
                        "type": media_type_to_agent(r.get("type")),
                        "season": r.get("season"),
                        "tmdb_id": r.get("tmdb_id"),
                        "imdb_id": r.get("imdb_id"),
                        "douban_id": r.get("douban_id"),
                        "bangumi_id": r.get("bangumi_id"),
                        "anilist_id": r.get("anilist_id"),
                        "media_source": r.get("source"),
                        "media_id": r.get("media_id"),
                        "vote_average": r.get("vote_average"),
                        "poster_path": r.get("poster_path"),
                        "detail_link": r.get("detail_link"),
                    }
                    simplified_results.append(simplified)
                result_json = json.dumps(
                    simplified_results, ensure_ascii=False, indent=2
                )
                has_more = total_count > page_size
                payload_msg = f"第 {page} 页，当前页 {len(simplified_results)} 条结果。"
                if has_more:
                    payload_msg += (
                        f" 可能有更多数据，可使用 page={page + 1} 获取下一页。"
                    )
                return f"{payload_msg}\n\n{result_json}"
            return "未找到推荐内容。"
        except Exception as e:
            logger.error(f"获取推荐失败: {e}", exc_info=True)
            return f"获取推荐时发生错误: {str(e)}"
