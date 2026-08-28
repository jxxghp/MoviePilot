"""搜索参数与结果缓存适配 owner。"""

from typing import Any, Dict, List, Optional, cast

from app.application.search.state import (
    SearchStateService,
    normalize_search_params,
    stringify_sites,
)
from app.chain.search.contract import _SearchOwnerBase
from app.domain.context import Context, SubtitleInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
)


class SearchCacheOwner(_SearchOwnerBase):
    """搜索参数与结果缓存适配 owner。"""

    @staticmethod
    def _stringify_sites(sites: Optional[List[int]]) -> str:
        """
        将站点ID列表转换为前端可直接复用的查询字符串。
        """
        return stringify_sites(sites)

    @staticmethod
    def _normalize_search_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """
        规范化上次搜索参数，供前端结果页重新搜索使用；旧复合关键字仅在
        缓存读取边界转换为独立的媒体来源和原生 ID。
        """
        return normalize_search_params(params)

    def _search_state(self) -> SearchStateService:
        """构造绑定当前 Chain 缓存端口的搜索状态服务。"""
        return SearchStateService(
            save_cache=self.save_cache,
            load_cache=self.load_cache,
            async_save_cache=self.async_save_cache,
            async_load_cache=self.async_load_cache,
            params_key=self._SEARCH_PARAMS_CACHE_KEY,
            result_key=self._RESULT_CACHE_KEY,
            subtitle_result_key=self._SUBTITLE_RESULT_CACHE_KEY,
        )

    def save_last_search_params(
        self,
        *,
        keyword: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        title: Optional[str] = None,
        year: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        music_type: Optional[str] = None,
        result_type: Optional[str] = "torrent",
    ) -> None:
        """
        保存最后一次资源搜索参数，标题搜索与精确身份搜索使用互斥字段。
        """
        self._search_state().save_params(
            keyword=keyword,
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            title=title,
            year=year,
            season=season,
            episode=episode,
            sites=sites,
            music_type=music_type,
            result_type=result_type,
        )

    async def async_save_last_search_params(
        self,
        *,
        keyword: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        title: Optional[str] = None,
        year: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        music_type: Optional[str] = None,
        result_type: Optional[str] = "torrent",
    ) -> None:
        """
        异步保存最后一次资源搜索参数，标题搜索与精确身份搜索使用互斥字段。
        """
        await self._search_state().async_save_params(
            keyword=keyword,
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            title=title,
            year=year,
            season=season,
            episode=episode,
            sites=sites,
            music_type=music_type,
            result_type=result_type,
        )

    def last_search_params(self) -> Optional[Dict[str, str]]:
        """
        获取上次搜索使用的参数。
        """
        return self._search_state().load_params()

    async def async_last_search_params(self) -> Optional[Dict[str, str]]:
        """
        异步获取上次搜索使用的参数。
        """
        return await self._search_state().async_load_params()

    def last_search_results(self) -> Optional[List[Context]]:
        """
        获取上次搜索结果
        """
        return cast(Optional[List[Context]], self._search_state().load_results())

    async def async_last_search_results(self) -> Optional[List[Context]]:
        """
        异步获取上次搜索结果
        """
        return cast(
            Optional[List[Context]],
            await self._search_state().async_load_results(),
        )

    async def async_last_subtitle_search_results(self) -> Optional[List[SubtitleInfo]]:
        """
        异步获取上次字幕搜索结果。
        """
        return cast(
            Optional[List[SubtitleInfo]],
            await self._search_state().async_load_subtitle_results(),
        )

    def _save_results(self, results: List[Context]) -> None:
        """通过搜索状态 owner 保存同步资源结果，避免业务入口直写缓存键。"""
        self._search_state().save_results(results)

    async def _async_save_results(self, results: List[Context]) -> None:
        """通过搜索状态 owner 保存异步资源结果，统一结果发布边界。"""
        await self._search_state().async_save_results(results)

    async def _async_save_subtitles(self, subtitles: List[SubtitleInfo]) -> None:
        """通过搜索状态 owner 保存字幕结果，统一字幕发布边界。"""
        await self._search_state().async_save_subtitle_results(subtitles)
