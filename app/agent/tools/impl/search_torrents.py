"""搜索种子工具"""

import json
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.search import SearchChain
from app.db.oper.systemconfig import SystemConfigOper
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.runtime.log import logger
from app.schemas.types import MediaSource, MediaType, SystemConfigKey
from app.domain.media import normalize_music_type
from ._torrent_search_utils import (
    SEARCH_RESULT_CACHE_FILE,
    build_filter_options,
)


class SearchTorrentsInput(BaseModel):
    """搜索种子工具的输入参数模型"""
    media_source: MediaSource = Field(..., description="Media metadata source")
    media_id: str = Field(..., description="Native ID for media_source")
    media_type: Optional[str] = Field(None, description="Allowed values: movie, tv, music")
    music_type: Optional[str] = Field(
        None,
        description="Music target entity: recording or album. Artists cannot be searched as torrent targets",
    )
    area: Optional[str] = Field(None, description="Search scope: 'title' (default) or 'imdbid'")
    sites: Optional[List[int]] = Field(None,
                                       description="Array of specific site IDs to search on (optional, if not provided searches all configured sites)")


class SearchTorrentsTool(MoviePilotTool):
    """按稳定媒体身份搜索影视、单曲或整张专辑资源。"""

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
        "Accepts one MediaSource enum value and its source-native media ID. "
        "Music targets are one recording or one complete album; artists are browse-only.")
    args_schema: Type[BaseModel] = SearchTorrentsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        media_type = kwargs.get("media_type")
        source = kwargs.get("media_source")
        media_id = kwargs.get("media_id")
        message = f"搜索种子: {source}={media_id}" if source and media_id else "搜索种子"
        if media_type:
            message += f" [{media_type}]"
        return message

    @staticmethod
    def _load_configured_sites() -> List[int]:
        """同步读取默认搜索站点列表。"""
        return SystemConfigOper().get(SystemConfigKey.IndexerSites) or []

    async def run(self, media_source: MediaSource, media_id: str,
                  media_type: Optional[str] = None, area: Optional[str] = None,
                  sites: Optional[List[int]] = None,
                  music_type: Optional[str] = None, **kwargs) -> str:
        """执行精确资源搜索并缓存带完整音乐上下文的候选。"""
        logger.info(
            f"执行工具: {self.name}, 参数: media_source={media_source}, "
            f"media_id={media_id}, media_type={media_type}, area={area}, sites={sites}"
        )

        if not media_source or not str(media_id or "").strip():
            return "参数错误：media_source 和 media_id 必须同时提供。"

        try:
            search_chain = SearchChain()
            media_type_enum = None
            if media_type:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'music'"

            normalized_music_type = None
            if music_type:
                normalized_music_type = normalize_music_type(
                    music_type,
                    allow_artist=False,
                )
                if not normalized_music_type:
                    return (
                        f"错误：无效的音乐实体类型 '{music_type}'，"
                        "支持的类型：'recording', 'album'"
                    )
                if media_type_enum != MediaType.MUSIC:
                    return "错误：music_type 仅能与 media_type='music' 一起使用"
            if media_type_enum == MediaType.MUSIC:
                if area == "imdbid":
                    return "错误：音乐不支持按 IMDb ID 搜索"

            filtered_torrents = await search_chain.async_search_by_id(
                media_source=media_source,
                media_id=media_id,
                mtype=media_type_enum,
                music_type=normalized_music_type,
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
                result_json = json.dumps({
                    "message": f"未找到相关种子资源: {media_source}:{media_id}",
                    "all_sites": all_sites,
                    "search_site_ids": search_site_ids,
                }, ensure_ascii=False, indent=2)
                return result_json
        except Exception as e:
            error_message = f"搜索种子时发生错误: {str(e)}"
            logger.error(f"搜索种子失败: {e}", exc_info=True)
            return error_message
