"""搜索种子工具"""

import json
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.search import SearchChain
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper  # noqa
from app.log import logger
from app.schemas.types import MediaType, SystemConfigKey
from ._torrent_search_utils import (
    SEARCH_RESULT_CACHE_FILE,
    build_filter_options,
)


class SearchTorrentsInput(BaseModel):
    """搜索种子工具的输入参数模型"""
    tmdb_id: Optional[int] = Field(None, description="TMDB media ID")
    douban_id: Optional[str] = Field(None, description="Douban media ID")
    bangumi_id: Optional[int] = Field(None, description="Bangumi media ID")
    anilist_id: Optional[int] = Field(None, description="AniList media ID")
    media_source: Optional[str] = Field(None, description="Media metadata source")
    media_id: Optional[str] = Field(None, description="Native ID for media_source")
    media_type: Optional[str] = Field(None, description="Allowed values: movie, tv")
    area: Optional[str] = Field(None, description="Search scope: 'title' (default) or 'imdbid'")
    sites: Optional[List[int]] = Field(None,
                                       description="Array of specific site IDs to search on (optional, if not provided searches all configured sites)")


class SearchTorrentsTool(MoviePilotTool):
    name: str = "search_torrents"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Resource,
        ToolTag.Site,
        ToolTag.Media,
    ]
    description: str = (
        "Search for torrent files by media ID across configured indexer sites, cache the matched results, "
        "and return available filter options for follow-up selection. "
        "Accepts a TMDB, Douban, Bangumi, AniList, or source-native media ID for accurate matching.")
    args_schema: Type[BaseModel] = SearchTorrentsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
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
        message = f"搜索种子: {label}={identity}" if label else "搜索种子"
        if media_type:
            message += f" [{media_type}]"
        return message

    @staticmethod
    def _load_configured_sites() -> List[int]:
        """同步读取默认搜索站点列表。"""
        return SystemConfigOper().get(SystemConfigKey.IndexerSites) or []

    async def run(self, tmdb_id: Optional[int] = None, douban_id: Optional[str] = None,
                  bangumi_id: Optional[int] = None, anilist_id: Optional[int] = None,
                  media_source: Optional[str] = None, media_id: Optional[str] = None,
                  media_type: Optional[str] = None, area: Optional[str] = None,
                  sites: Optional[List[int]] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: tmdb_id={tmdb_id}, douban_id={douban_id}, media_type={media_type}, area={area}, sites={sites}")

        if not any((tmdb_id, douban_id, bangumi_id, anilist_id, media_id)):
            return "参数错误：至少需要提供一个媒体 ID，请先使用 search_media 工具获取媒体信息。"

        try:
            search_chain = SearchChain()
            media_type_enum = None
            if media_type:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"

            filtered_torrents = await search_chain.async_search_by_id(
                tmdbid=tmdb_id,
                doubanid=douban_id,
                bangumiid=bangumi_id,
                anilistid=anilist_id,
                source=media_source,
                mediaid=media_id,
                mtype=media_type_enum,
                area=area or "title",
                sites=sites,
                cache_local=False,
            )

            # 获取站点信息
            all_indexers = await SitesHelper().async_get_indexers()
            all_sites = [{"id": indexer.get("id"), "name": indexer.get("name")} for indexer in (all_indexers or [])]

            if sites:
                search_site_ids = sites
            else:
                search_site_ids = self._load_configured_sites()

            if filtered_torrents:
                await search_chain.async_save_cache(filtered_torrents, SEARCH_RESULT_CACHE_FILE)
                result_json = json.dumps({
                    "total_count": len(filtered_torrents),
                    "message": "搜索完成。请使用 get_search_results 工具获取搜索结果。",
                    "all_sites": all_sites,
                    "search_site_ids": search_site_ids,
                    "filter_options": build_filter_options(filtered_torrents),
                }, ensure_ascii=False, indent=2)
                return result_json
            else:
                identity = media_id or tmdb_id or douban_id or bangumi_id or anilist_id
                result_json = json.dumps({
                    "message": f"未找到相关种子资源: {identity}",
                    "all_sites": all_sites,
                    "search_site_ids": search_site_ids,
                }, ensure_ascii=False, indent=2)
                return result_json
        except Exception as e:
            error_message = f"搜索种子时发生错误: {str(e)}"
            logger.error(f"搜索种子失败: {e}", exc_info=True)
            return error_message
