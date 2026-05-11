"""查询媒体详情工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.log import logger
from app.schemas.types import MediaType

DIRECTOR_PREVIEW_LIMIT = 10
ACTOR_PREVIEW_LIMIT = 20
SEASON_PREVIEW_LIMIT = 100


class QueryMediaDetailInput(BaseModel):
    """查询媒体详情工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    tmdb_id: Optional[int] = Field(None, description="TMDB ID of the media (movie or TV series, can be obtained from search_media tool)")
    douban_id: Optional[str] = Field(None, description="Douban ID of the media (alternative to tmdb_id)")
    media_type: str = Field(..., description="Allowed values: movie, tv")


class QueryMediaDetailTool(MoviePilotTool):
    name: str = "query_media_detail"
    description: str = "Query supplementary media details from TMDB by ID and media_type. Accepts tmdb_id or douban_id (at least one required). media_type accepts 'movie' or 'tv'. Returns non-duplicated detail fields such as status, genres, directors, actors, and season info for TV series."
    args_schema: Type[BaseModel] = QueryMediaDetailInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        tmdb_id = kwargs.get("tmdb_id")
        douban_id = kwargs.get("douban_id")
        if tmdb_id:
            return f"查询媒体详情: TMDB ID {tmdb_id}"
        return f"查询媒体详情: 豆瓣 ID {douban_id}"

    async def run(self, media_type: str, tmdb_id: Optional[int] = None, douban_id: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: tmdb_id={tmdb_id}, douban_id={douban_id}, media_type={media_type}")

        if tmdb_id is None and douban_id is None:
            return json.dumps({
                "success": False,
                "message": "必须提供 tmdb_id 或 douban_id 之一"
            }, ensure_ascii=False)

        try:
            media_chain = MediaChain()

            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return json.dumps({
                    "success": False,
                    "message": f"无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"
                }, ensure_ascii=False)

            mediainfo = await media_chain.async_recognize_media(tmdbid=tmdb_id, doubanid=douban_id, mtype=media_type_enum)

            if not mediainfo:
                id_info = f"TMDB ID {tmdb_id}" if tmdb_id else f"豆瓣 ID {douban_id}"
                return json.dumps({
                    "success": False,
                    "message": f"未找到 {id_info} 的媒体信息"
                }, ensure_ascii=False)

            # 精简 genres - 只保留名称
            genres = [g.get("name") for g in (mediainfo.genres or []) if g.get("name")]

            # 精简 directors - 只保留姓名和职位
            director_source = [d for d in (mediainfo.directors or []) if d.get("name")]
            directors = [
                {
                    "name": d.get("name"),
                    "job": d.get("job")
                }
                for d in director_source[:DIRECTOR_PREVIEW_LIMIT]
            ]

            # 精简 actors - 只保留姓名和角色
            actor_source = [a for a in (mediainfo.actors or []) if a.get("name")]
            actors = [
                {
                    "name": a.get("name"),
                    "character": a.get("character")
                }
                for a in actor_source[:ACTOR_PREVIEW_LIMIT]
            ]

            # 构建基础媒体详情信息
            result = {
                "status": mediainfo.status,
                "genres": genres,
                "directors": directors,
                "directors_total": len(director_source),
                "directors_truncated": len(director_source) > DIRECTOR_PREVIEW_LIMIT,
                "actors": actors,
                "actors_total": len(actor_source),
                "actors_truncated": len(actor_source) > ACTOR_PREVIEW_LIMIT,
            }

            # 如果是电视剧，添加电视剧特有信息
            if mediainfo.type == MediaType.TV:
                # 精简 season_info - 只保留基础摘要
                season_source = [
                    s for s in (mediainfo.season_info or [])
                    if s.get("season_number") is not None
                ]
                season_info = [
                    {
                        "season_number": s.get("season_number"),
                        "name": s.get("name"),
                        "episode_count": s.get("episode_count"),
                        "air_date": s.get("air_date")
                    }
                    for s in season_source[:SEASON_PREVIEW_LIMIT]
                ]

                result.update({
                    "number_of_seasons": mediainfo.number_of_seasons,
                    "number_of_episodes": mediainfo.number_of_episodes,
                    "first_air_date": mediainfo.first_air_date,
                    "last_air_date": mediainfo.last_air_date,
                    "season_info": season_info,
                    "season_info_total": len(season_source),
                    "season_info_truncated": len(season_source) > SEASON_PREVIEW_LIMIT,
                })

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"查询媒体详情失败: {str(e)}"
            logger.error(f"查询媒体详情失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message,
                "tmdb_id": tmdb_id,
                "douban_id": douban_id
            }, ensure_ascii=False)
