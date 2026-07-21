"""查询媒体库工具"""

import asyncio
import json
from collections import OrderedDict
from typing import Optional, Type, Any

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.mediaserver import MediaServerChain
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType, media_type_to_agent


def _sort_seasons(seasons: Optional[dict]) -> dict:
    """按季号、集号升序整理季集信息，保证输出稳定。"""
    if not seasons:
        return {}

    def _sort_key(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)

    return OrderedDict(
        (season, sorted(episodes, key=_sort_key))
        for season, episodes in sorted(seasons.items(), key=lambda item: _sort_key(item[0]))
    )


def _filter_regular_seasons(seasons: Optional[dict]) -> OrderedDict:
    """仅保留正片季，忽略 season 0 等特殊季。"""
    sorted_seasons = _sort_seasons(seasons)
    regular_seasons = OrderedDict()
    for season, episodes in sorted_seasons.items():
        try:
            season_number = int(season)
        except (TypeError, ValueError):
            continue
        if season_number > 0:
            regular_seasons[season_number] = episodes
    return regular_seasons


def _build_tv_server_result(existing_seasons: OrderedDict, total_seasons: OrderedDict) -> dict[str, Any]:
    """构建单个服务器的电视剧存在性结果。"""
    seasons_result = OrderedDict()
    missing_seasons = []
    all_seasons = sorted(set(total_seasons.keys()) | set(existing_seasons.keys()))

    for season in all_seasons:
        existing_episodes = existing_seasons.get(season, [])
        total_episodes = total_seasons.get(season)
        if total_episodes is not None:
            missing_episodes = [episode for episode in total_episodes if episode not in existing_episodes]
            total_episode_count = len(total_episodes)
        else:
            missing_episodes = None
            total_episode_count = None
        seasons_result[str(season)] = {
            "existing_episodes": existing_episodes,
            "total_episodes": total_episode_count,
            "missing_episodes": missing_episodes
        }
        if total_episodes is not None and not existing_episodes:
            missing_seasons.append(season)

    return {
        "seasons": seasons_result,
        "missing_seasons": missing_seasons
    }


class QueryLibraryExistsInput(BaseModel):
    """查询媒体库工具的输入参数模型"""
    tmdb_id: Optional[int] = Field(None, description="TMDB media ID")
    douban_id: Optional[str] = Field(None, description="Douban media ID")
    bangumi_id: Optional[int] = Field(None, description="Bangumi media ID")
    anilist_id: Optional[int] = Field(None, description="AniList media ID")
    media_source: Optional[str] = Field(None, description="Media metadata source")
    media_id: Optional[str] = Field(None, description="Native ID for media_source")
    media_type: Optional[str] = Field(None, description="Allowed values: movie, tv")


class QueryLibraryExistsTool(MoviePilotTool):
    name: str = "query_library_exists"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Library,
        ToolTag.Media,
    ]
    description: str = "Check whether media already exists in Plex, Emby, or Jellyfin by a TMDB, Douban, Bangumi, AniList, or source-native media ID. Results are grouped by media server; TV results include existing episodes, total episodes, and missing episodes/seasons."
    args_schema: Type[BaseModel] = QueryLibraryExistsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        media_type = kwargs.get("media_type")
        identities = (
            ("TMDB", kwargs.get("tmdb_id")),
            ("豆瓣", kwargs.get("douban_id")),
            ("Bangumi", kwargs.get("bangumi_id")),
            ("AniList", kwargs.get("anilist_id")),
            (kwargs.get("media_source") or "媒体源", kwargs.get("media_id")),
        )
        label, identity = next(
            ((label, identity) for label, identity in identities if identity is not None),
            (None, None),
        )
        message = f"查询媒体库: {label}={identity}" if label else "查询媒体库"
        if media_type:
            message += f" [{media_type}]"
        return message

    @staticmethod
    def _get_media_server_names() -> list[str]:
        """同步读取已加载媒体服务器名称。"""
        return sorted(MediaServerHelper().get_services().keys())

    @staticmethod
    def _query_media_exists(mediainfo, server: Optional[str] = None):
        """同步查询单个媒体服务器的存在性信息。"""
        return MediaServerChain().media_exists(mediainfo=mediainfo, server=server)

    async def run(self, tmdb_id: Optional[int] = None, douban_id: Optional[str] = None,
                  bangumi_id: Optional[int] = None, anilist_id: Optional[int] = None,
                  media_source: Optional[str] = None, media_id: Optional[str] = None,
                  media_type: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: tmdb_id={tmdb_id}, douban_id={douban_id}, media_type={media_type}")
        try:
            if not any((tmdb_id, douban_id, bangumi_id, anilist_id, media_id)):
                return "参数错误：至少需要提供一个媒体 ID，请先使用 search_media 工具获取媒体信息。"

            media_type_enum = None
            if media_type:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"

            media_chain = MediaServerChain()
            mediainfo = await media_chain.async_recognize_media(
                tmdbid=tmdb_id,
                doubanid=douban_id,
                bangumiid=bangumi_id,
                anilistid=anilist_id,
                source=media_source,
                mediaid=media_id,
                mtype=media_type_enum,
            )
            if not mediainfo:
                identity = media_id or tmdb_id or douban_id or bangumi_id or anilist_id
                return f"未识别到媒体信息: {identity}"

            # 2. 遍历所有媒体服务器，分别查询存在性信息
            server_results = OrderedDict()
            total_seasons = _filter_regular_seasons(mediainfo.seasons)
            service_names = self._get_media_server_names()

            server_checks = await asyncio.gather(
                *[
                    self.run_blocking(
                        "mediaserver",
                        self._query_media_exists,
                        mediainfo,
                        service_name,
                    )
                    for service_name in service_names
                ]
            )

            for service_name, existsinfo in zip(service_names, server_checks):
                if not existsinfo:
                    continue

                if existsinfo.type == MediaType.TV:
                    existing_seasons = _filter_regular_seasons(existsinfo.seasons)
                    server_results[service_name] = _build_tv_server_result(
                        existing_seasons=existing_seasons,
                        total_seasons=total_seasons
                    )
                else:
                    server_results[service_name] = {
                        "exists": True
                    }

            if not server_results:
                global_existsinfo = await self.run_blocking(
                    "mediaserver", self._query_media_exists, mediainfo, None
                )
                if not global_existsinfo:
                    return "媒体库中未找到相关媒体"

                fallback_server_name = global_existsinfo.server or "local"
                if global_existsinfo.type == MediaType.TV:
                    server_results[fallback_server_name] = _build_tv_server_result(
                        existing_seasons=_filter_regular_seasons(global_existsinfo.seasons),
                        total_seasons=total_seasons
                    )
                else:
                    server_results[fallback_server_name] = {
                        "exists": True
                    }

            # 3. 组装统一的存在性结果，不查询媒体服务器详情
            result_dict = {
                "title": mediainfo.title,
                "year": mediainfo.year,
                "type": media_type_to_agent(mediainfo.type),
                "servers": server_results
            }

            return json.dumps([result_dict], ensure_ascii=False)
        except Exception as e:
            logger.error(f"查询媒体库失败: {e}", exc_info=True)
            return f"查询媒体库时发生错误: {str(e)}"
