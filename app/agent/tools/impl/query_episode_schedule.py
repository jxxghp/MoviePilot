"""查询剧集上映时间工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.orchestration.tmdb import TmdbChain
from app.runtime.log import logger


class QueryEpisodeScheduleInput(BaseModel):
    """查询剧集上映时间工具的输入参数模型"""
    tmdb_id: int = Field(..., description="TMDB ID of the TV series (can be obtained from search_media tool)")
    season: int = Field(..., description="Season number to query")
    episode_group: Optional[str] = Field(None, description="Episode group ID (optional)")


class QueryEpisodeScheduleTool(MoviePilotTool):
    name: str = "query_episode_schedule"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
    ]
    description: str = "Query TV series episode air dates and schedule. Returns non-duplicated schedule fields, including episode list, air-date statistics, and per-episode metadata. Filters out episodes without air dates."
    args_schema: Type[BaseModel] = QueryEpisodeScheduleInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        tmdb_id = kwargs.get("tmdb_id")
        season = kwargs.get("season")
        episode_group = kwargs.get("episode_group")

        message = f"查询剧集上映时间: TMDB ID {tmdb_id} 第{season}季"
        if episode_group:
            message += f" (剧集组: {episode_group})"

        return message

    async def run(self, tmdb_id: int, season: int, episode_group: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: tmdb_id={tmdb_id}, season={season}, episode_group={episode_group}")

        try:
            # 获取集列表
            tmdb_chain = TmdbChain()
            episodes = await tmdb_chain.async_tmdb_episodes(
                tmdbid=tmdb_id,
                season=season,
                episode_group=episode_group
            )

            if not episodes:
                return json.dumps({
                    "success": False,
                    "message": f"未找到 TMDB ID {tmdb_id} 第{season}季的集信息"
                }, ensure_ascii=False)

            # 过滤掉没有上映日期的集，并构建每集的详细信息
            episode_list = []
            for episode in episodes:
                air_date = episode.air_date
                
                # 过滤掉没有上映日期的数据
                if not air_date:
                    continue
                
                episode_info = {
                    "episode_number": episode.episode_number,
                    "name": episode.name,
                    "air_date": air_date,
                    "runtime": episode.runtime,
                    "vote_average": episode.vote_average,
                    "still_path": episode.still_path,
                    "episode_type": episode.episode_type,
                    "season_number": episode.season_number
                }
                episode_list.append(episode_info)

            if not episode_list:
                return json.dumps({
                    "success": False,
                    "message": f"未找到 TMDB ID {tmdb_id} 第{season}季的播出时间信息（所有集都没有播出日期）"
                }, ensure_ascii=False)

            # 按播出日期排序
            episode_list.sort(key=lambda x: (x["air_date"] or "", x["episode_number"] or 0))

            result = {
                "season": season,
                "total_episodes": len(episodes),
                "episodes_with_air_date": len(episode_list),
                "episodes": episode_list
            }

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"查询剧集上映时间失败: {str(e)}"
            logger.error(f"查询剧集上映时间失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message,
                "tmdb_id": tmdb_id,
                "season": season
            }, ensure_ascii=False)
