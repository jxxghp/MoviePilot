"""查询热门订阅工具"""

import json
from typing import Optional, Type

import cn2an
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.core.context import MediaInfo
from app.helper.subscribe import SubscribeHelper
from app.log import logger
from app.schemas.types import MediaType, media_type_to_agent

MAX_PAGE_SIZE = 50


class QueryPopularSubscribesInput(BaseModel):
    """查询热门订阅工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    media_type: str = Field(..., description="Allowed values: movie, tv")
    page: Optional[int] = Field(1, description="Page number for pagination (default: 1)")
    count: Optional[int] = Field(30, description="Number of items per page (default: 30, max: 50)")
    min_sub: Optional[int] = Field(None, description="Minimum number of subscribers filter (optional, e.g., 5)")
    genre_id: Optional[int] = Field(None, description="Filter by genre ID (optional)")
    min_rating: Optional[float] = Field(None, description="Minimum rating filter (optional, e.g., 7.5)")
    max_rating: Optional[float] = Field(None, description="Maximum rating filter (optional, e.g., 10.0)")
    sort_type: Optional[str] = Field(None, description="Sort type (optional, e.g., 'count', 'rating')")


class QueryPopularSubscribesTool(MoviePilotTool):
    name: str = "query_popular_subscribes"
    description: str = "Query popular subscriptions based on user shared data. Shows media with the most subscribers, supports filtering by genre, rating, minimum subscribers, and pagination."
    args_schema: Type[BaseModel] = QueryPopularSubscribesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        media_type = kwargs.get("media_type", "")
        page = kwargs.get("page", 1)
        min_sub = kwargs.get("min_sub")
        min_rating = kwargs.get("min_rating")
        max_rating = kwargs.get("max_rating")
        
        parts = [f"查询热门订阅 [{media_type}]"]
        
        if min_sub:
            parts.append(f"最少订阅: {min_sub}")
        if min_rating:
            parts.append(f"最低评分: {min_rating}")
        if max_rating:
            parts.append(f"最高评分: {max_rating}")
        if page > 1:
            parts.append(f"第{page}页")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, media_type: str,
                  page: Optional[int] = 1,
                  count: Optional[int] = 30,
                  min_sub: Optional[int] = None,
                  genre_id: Optional[int] = None,
                  min_rating: Optional[float] = None,
                  max_rating: Optional[float] = None,
                  sort_type: Optional[str] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: media_type={media_type}, page={page}, count={count}, min_sub={min_sub}, "
            f"genre_id={genre_id}, min_rating={min_rating}, max_rating={max_rating}, sort_type={sort_type}")

        try:
            if page is None or page < 1:
                page = 1
            if count is None or count < 1:
                count = 30
            # 外部统计接口支持传入 count，这里做硬上限，避免 Agent 一次拉取过多结果。
            count = min(count, MAX_PAGE_SIZE)
            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"

            subscribe_helper = SubscribeHelper()
            subscribes = await subscribe_helper.async_get_statistic(
                stype=media_type_enum.to_agent(),
                page=page,
                count=count,
                genre_id=genre_id,
                min_rating=min_rating,
                max_rating=max_rating,
                sort_type=sort_type
            )

            if not subscribes:
                return "未找到热门订阅数据（可能订阅统计功能未启用）"

            # 转换为MediaInfo格式并过滤
            ret_medias = []
            for sub in subscribes:
                # 订阅人数
                subscriber_count = sub.get("count", 0)
                # 如果设置了最小订阅人数，进行过滤
                if min_sub and subscriber_count < min_sub:
                    continue

                media = MediaInfo()
                raw_type = str(sub.get("type") or "").strip().lower()
                if raw_type in ["movie", "电影"]:
                    media.type = MediaType.MOVIE
                elif raw_type in ["tv", "电视剧"]:
                    media.type = MediaType.TV
                else:
                    # 跳过无法识别类型的数据，避免单条脏数据导致整批失败
                    logger.warning(f"跳过未知媒体类型: {sub.get('type')}")
                    continue
                media.tmdb_id = sub.get("tmdbid")
                # 处理标题
                title = sub.get("name")
                season = sub.get("season")
                if season and int(season) > 1 and media.tmdb_id:
                    # 小写数据转大写
                    season_str = cn2an.an2cn(season, "low")
                    title = f"{title} 第{season_str}季"
                media.title = title
                media.year = sub.get("year")
                media.douban_id = sub.get("doubanid")
                media.bangumi_id = sub.get("bangumiid")
                media.tvdb_id = sub.get("tvdbid")
                media.imdb_id = sub.get("imdbid")
                media.season = sub.get("season")
                media.vote_average = sub.get("vote")
                media.poster_path = sub.get("poster")
                media.backdrop_path = sub.get("backdrop")
                media.popularity = subscriber_count
                ret_medias.append(media)

            if not ret_medias:
                return "未找到符合条件的热门订阅"

            # 转换为字典格式，只保留关键信息
            simplified_medias = []
            for media in ret_medias:
                media_dict = media.to_dict()
                simplified = {
                    "type": media_type_to_agent(media_dict.get("type")),
                    "title": media_dict.get("title"),
                    "year": media_dict.get("year"),
                    "tmdb_id": media_dict.get("tmdb_id"),
                    "douban_id": media_dict.get("douban_id"),
                    "bangumi_id": media_dict.get("bangumi_id"),
                    "tvdb_id": media_dict.get("tvdb_id"),
                    "imdb_id": media_dict.get("imdb_id"),
                    "season": media_dict.get("season"),
                    "vote_average": media_dict.get("vote_average"),
                    "poster_path": media_dict.get("poster_path"),
                    "backdrop_path": media_dict.get("backdrop_path"),
                    "popularity": media_dict.get("popularity"),  # 订阅人数
                    "subscriber_count": media_dict.get("popularity")  # 明确标注为订阅人数
                }
                simplified_medias.append(simplified)

            result_json = json.dumps(simplified_medias, ensure_ascii=False, indent=2)

            pagination_info = f"第 {page} 页，每页 {count} 条，共 {len(simplified_medias)} 条结果"

            return f"{pagination_info}\n\n{result_json}"
        except Exception as e:
            logger.error(f"查询热门订阅失败: {e}", exc_info=True)
            return f"查询热门订阅时发生错误: {str(e)}"
