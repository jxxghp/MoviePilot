"""搜索处理链稳定 Facade。"""

import threading
from collections.abc import AsyncIterator, Callable
from typing import Any, Optional, TypeVar, cast

from app.application.subscription.sitebudget import SubscriptionSiteBudget
from app.chain.base import ChainBase
from app.chain.search.cache import SearchCacheOwner
from app.chain.search.media import SearchMediaOwner
from app.chain.search.music import SearchMusicOwner
from app.chain.search.pagination import SearchPaginationOwner
from app.chain.search.plan import SearchPlanOwner
from app.chain.search.provider import SearchProviderOwner
from app.chain.search.recommend import SearchRecommendOwner
from app.chain.search.result import SearchResultOwner
from app.chain.search.site import SearchSiteOwner
from app.chain.search.subtitle import SearchSubtitleOwner
from app.chain.search.title import SearchTitleOwner
from app.domain.context import Context, MediaInfo, SubtitleInfo
from app.runtime.events import Event, eventmanager
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import EventType, MediaSource, MediaType

_PUBLIC_MODULE = "app.chain.search"
_Handler = TypeVar("_Handler", bound=Callable[..., Any])


def _public_handler(handler: _Handler) -> _Handler:
    """在事件注册前恢复插件可见的稳定模块身份。"""
    handler.__module__ = _PUBLIC_MODULE
    return handler


class SearchChain(ChainBase):
    """组合搜索计划、provider、结果、音乐、字幕与推荐的稳定门面。"""

    __module__ = _PUBLIC_MODULE

    _RESULT_CACHE_KEY = "__search_result__"
    _SUBTITLE_RESULT_CACHE_KEY = "__subtitle_search_result__"
    _SEARCH_PARAMS_CACHE_KEY = "__search_params__"
    _AI_INDICES_CACHE_KEY = "__ai_recommend_indices__"

    def configure_subscription_site_budget(
        self,
        budget: Optional[SubscriptionSiteBudget],
    ) -> None:
        """仅为订阅搜索启用或清除站点预算，不影响其它搜索入口。"""
        self._subscription_site_budget = budget
        self._subscription_site_budget_failures: list[str] = []
        self._subscription_site_budget_failure_lock = threading.Lock()

    def record_subscription_site_budget_failure(self, error: str) -> None:
        """线程安全地记录一个站点执行失败，供订阅任务暴露聚合失败。"""
        lock = getattr(self, "_subscription_site_budget_failure_lock", None)
        if lock is None:
            return
        with lock:
            self._subscription_site_budget_failures.append(error)

    def consume_subscription_site_budget_failures(self) -> tuple[str, ...]:
        """读取并清空当前订阅搜索积累的站点预算失败。"""
        lock = getattr(self, "_subscription_site_budget_failure_lock", None)
        if lock is None:
            return ()
        with lock:
            failures = tuple(self._subscription_site_budget_failures)
            self._subscription_site_budget_failures.clear()
        return failures

    # owner descriptor 经类访问后被 mypy 视为普通 Callable；运行时仍需取回原始
    # classmethod 函数，才能保持 SearchChain 的直接 MRO 与既有绑定语义。
    music_site_keywords = classmethod(  # type: ignore[var-annotated]
        SearchMusicOwner.music_site_keywords.__func__  # type: ignore[attr-defined]
    )
    matches_music_resource = classmethod(  # type: ignore[var-annotated]
        SearchMusicOwner.matches_music_resource.__func__  # type: ignore[attr-defined]
    )
    _normalize_music_match_text = staticmethod(SearchMusicOwner._normalize_music_match_text)
    _unique_music_texts = staticmethod(SearchMusicOwner._unique_music_texts)
    _get_search_resource_pages = staticmethod(SearchPaginationOwner._get_search_resource_pages)
    _build_search_pages = classmethod(  # type: ignore[var-annotated]
        SearchPaginationOwner._build_search_pages.__func__  # type: ignore[attr-defined]
    )
    _should_continue_search_pages = SearchPaginationOwner._should_continue_search_pages
    _should_continue_subtitle_search_pages = staticmethod(SearchPaginationOwner._should_continue_subtitle_search_pages)
    @property
    def is_ai_recommend_enabled(self) -> bool:
        """经推荐 owner 的原始描述符读取启用状态，避免 Facade 属性递归。"""
        descriptor = cast(
            property,
            SearchRecommendOwner.__dict__["is_ai_recommend_enabled"],
        )
        return cast(bool, descriptor.__get__(self, type(self)))

    _calculate_recommend_request_hash = staticmethod(SearchRecommendOwner._calculate_recommend_request_hash)
    _build_ai_recommend_status = SearchRecommendOwner._build_ai_recommend_status
    def get_current_recommend_status_only(self) -> dict[str, Any]:
        """返回当前推荐状态，不改变推荐请求代际。"""
        return SearchRecommendOwner.get_current_recommend_status_only(
            cast(SearchRecommendOwner, self)
        )

    def get_recommend_status(
        self,
        filtered_indices: Optional[list[int]],
        search_results_count: int,
    ) -> dict[str, Any]:
        """按当前筛选条件返回推荐状态。"""
        return SearchRecommendOwner.get_recommend_status(
            cast(SearchRecommendOwner, self),
            filtered_indices=filtered_indices,
            search_results_count=search_results_count,
        )

    def cancel_ai_recommend(self) -> None:
        """委托推荐 owner 取消当前任务并清理推荐缓存。"""
        SearchRecommendOwner.cancel_ai_recommend(
            cast(SearchRecommendOwner, self)
        )

    _build_search_keyword = staticmethod(SearchPlanOwner._build_search_keyword)
    _media_recognize_kwargs = staticmethod(SearchPlanOwner._media_recognize_kwargs)
    _stringify_sites = staticmethod(SearchCacheOwner._stringify_sites)
    _normalize_search_params = staticmethod(SearchCacheOwner._normalize_search_params)
    _search_state = SearchCacheOwner._search_state
    _save_results = SearchCacheOwner._save_results
    _async_save_results = SearchCacheOwner._async_save_results
    _async_save_subtitles = SearchCacheOwner._async_save_subtitles
    save_last_search_params = SearchCacheOwner.save_last_search_params
    async_save_last_search_params = SearchCacheOwner.async_save_last_search_params
    def last_search_params(self) -> Optional[dict[str, str]]:
        """返回最近一次搜索参数。"""
        return SearchCacheOwner.last_search_params(cast(SearchCacheOwner, self))

    async def async_last_search_params(self) -> Optional[dict[str, str]]:
        """异步返回最近一次搜索参数。"""
        return await SearchCacheOwner.async_last_search_params(
            cast(SearchCacheOwner, self)
        )
    _normalize_ai_indices = staticmethod(SearchRecommendOwner._normalize_ai_indices)
    _extract_recommend_items = staticmethod(SearchRecommendOwner._extract_recommend_items)
    _restore_original_indices = staticmethod(SearchRecommendOwner._restore_original_indices)
    _invoke_recommend_llm = staticmethod(SearchRecommendOwner._invoke_recommend_llm)
    def start_recommend_task(
        self,
        filtered_indices: Optional[list[int]],
        search_results_count: int,
        results: list[Any],
    ) -> None:
        """委托推荐 owner 启动受代际保护的推荐任务。"""
        SearchRecommendOwner.start_recommend_task(
            cast(SearchRecommendOwner, self),
            filtered_indices=filtered_indices,
            search_results_count=search_results_count,
            results=results,
        )

    def search_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[list[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> list[Context]:
        """通过稳定 Facade 执行同步精确媒体搜索。"""
        return SearchMediaOwner.search_by_id(
            cast(SearchMediaOwner, self),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            cache_local=cache_local,
            music_type=music_type,
        )

    def search_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[list[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[list[str]] = None,
    ) -> list[Context]:
        """通过稳定 Facade 调用标题搜索 owner，保留精确的公开类型合同。"""
        return SearchTitleOwner.search_by_title(
            cast(SearchTitleOwner, self),
            title=title,
            page=page,
            sites=sites,
            cache_local=cache_local,
            mtype=mtype,
            rule_groups=rule_groups,
        )

    def last_search_results(self) -> Optional[list[Context]]:
        """返回最近一次资源搜索结果。"""
        return SearchCacheOwner.last_search_results(cast(SearchCacheOwner, self))

    async def async_last_search_results(self) -> Optional[list[Context]]:
        """异步返回最近一次资源搜索结果。"""
        return await SearchCacheOwner.async_last_search_results(
            cast(SearchCacheOwner, self)
        )

    async def async_last_subtitle_search_results(
        self,
    ) -> Optional[list[SubtitleInfo]]:
        """异步返回最近一次字幕搜索结果。"""
        return await SearchCacheOwner.async_last_subtitle_search_results(
            cast(SearchCacheOwner, self)
        )

    async def async_search_subtitles_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[list[int]] = None,
        cache_local: Optional[bool] = False,
    ) -> list[SubtitleInfo]:
        """通过稳定 Facade 执行异步标题字幕搜索。"""
        return await SearchSubtitleOwner.async_search_subtitles_by_title(
            cast(SearchSubtitleOwner, self),
            title=title,
            page=page,
            sites=sites,
            cache_local=cache_local,
        )

    async def async_search_subtitles_by_title_stream(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[list[int]] = None,
        cache_local: Optional[bool] = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过稳定 Facade 流式执行标题字幕搜索。"""
        async for event in SearchSubtitleOwner.async_search_subtitles_by_title_stream(
            cast(SearchSubtitleOwner, self),
            title=title,
            page=page,
            sites=sites,
            cache_local=cache_local,
        ):
            yield event

    async def async_search_subtitles_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[list[int]] = None,
        cache_local: bool = False,
    ) -> list[SubtitleInfo]:
        """通过稳定 Facade 执行异步精确字幕搜索。"""
        return await SearchSubtitleOwner.async_search_subtitles_by_id(
            cast(SearchSubtitleOwner, self),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            season=season,
            episode=episode,
            sites=sites,
            cache_local=cache_local,
        )

    async def async_search_subtitles_by_id_stream(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[list[int]] = None,
        cache_local: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过稳定 Facade 流式执行精确字幕搜索。"""
        async for event in SearchSubtitleOwner.async_search_subtitles_by_id_stream(
            cast(SearchSubtitleOwner, self),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            season=season,
            episode=episode,
            sites=sites,
            cache_local=cache_local,
        ):
            yield event

    async def async_search_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[list[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> list[Context]:
        """通过稳定 Facade 执行异步精确媒体搜索。"""
        return await SearchMediaOwner.async_search_by_id(
            cast(SearchMediaOwner, self),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            cache_local=cache_local,
            music_type=music_type,
        )

    async def async_search_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[list[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[list[str]] = None,
    ) -> list[Context]:
        """通过稳定 Facade 执行异步标题资源搜索。"""
        return await SearchTitleOwner.async_search_by_title(
            cast(SearchTitleOwner, self),
            title=title,
            page=page,
            sites=sites,
            cache_local=cache_local,
            mtype=mtype,
            rule_groups=rule_groups,
        )

    async def async_search_by_title_stream(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[list[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[list[str]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过稳定 Facade 流式执行标题资源搜索。"""
        async for event in SearchTitleOwner.async_search_by_title_stream(
            cast(SearchTitleOwner, self),
            title=title,
            page=page,
            sites=sites,
            cache_local=cache_local,
            mtype=mtype,
            rule_groups=rule_groups,
        ):
            yield event
    _build_title_search_meta = staticmethod(SearchTitleOwner._build_title_search_meta)
    _filter_title_search_torrents = SearchTitleOwner._filter_title_search_torrents
    async def async_search_by_id_stream(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[list[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过稳定 Facade 流式执行精确媒体搜索。"""
        async for event in SearchMediaOwner.async_search_by_id_stream(
            cast(SearchMediaOwner, self),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            cache_local=cache_local,
            music_type=music_type,
        ):
            yield event
    _prepare_params = staticmethod(SearchPlanOwner._prepare_params)
    _copy_media_input = staticmethod(SearchPlanOwner._copy_media_input)
    _parse_result = SearchResultOwner._parse_result
    _remove_duplicate = staticmethod(SearchResultOwner._remove_duplicate)
    _build_music_contexts = SearchMusicOwner._build_music_contexts
    _matching_music_torrents = staticmethod(SearchMusicOwner._matching_music_torrents)
    _process_music = SearchMusicOwner._process_music
    _async_process_music = SearchMusicOwner._async_process_music
    _async_process_music_stream = SearchMusicOwner._async_process_music_stream

    def process(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[dict[str, dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[list[int]] = None,
        rule_groups: Optional[list[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[list[str]] = None,
        filter_params: Optional[dict[str, str]] = None,
    ) -> list[Context]:
        """通过稳定 Facade 调用精确媒体搜索 owner，保留公开类型合同。"""
        return SearchMediaOwner.process(
            cast(SearchMediaOwner, self),
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists,
            sites=sites,
            rule_groups=rule_groups,
            area=area,
            custom_words=custom_words,
            filter_params=filter_params,
        )

    async def async_process(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[dict[str, dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[list[int]] = None,
        rule_groups: Optional[list[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[list[str]] = None,
        filter_params: Optional[dict[str, str]] = None,
    ) -> list[Context]:
        """通过稳定 Facade 执行异步媒体搜索编排。"""
        return await SearchMediaOwner.async_process(
            cast(SearchMediaOwner, self),
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists,
            sites=sites,
            rule_groups=rule_groups,
            area=area,
            custom_words=custom_words,
            filter_params=filter_params,
        )

    async def async_process_stream(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[dict[str, dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[list[int]] = None,
        rule_groups: Optional[list[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[list[str]] = None,
        filter_params: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """通过稳定 Facade 流式执行媒体搜索编排。"""
        async for event in SearchMediaOwner.async_process_stream(
            cast(SearchMediaOwner, self),
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists,
            sites=sites,
            rule_groups=rule_groups,
            area=area,
            custom_words=custom_words,
            filter_params=filter_params,
        ):
            yield event
    _build_subtitle_season_episodes = staticmethod(SearchSubtitleOwner._build_subtitle_season_episodes)
    _build_subtitle_torrent = staticmethod(SearchSubtitleOwner._build_subtitle_torrent)
    _build_subtitle_names = staticmethod(SearchSubtitleOwner._build_subtitle_names)
    _build_subtitle_meta = staticmethod(SearchSubtitleOwner._build_subtitle_meta)
    _match_subtitle_episode = staticmethod(SearchSubtitleOwner._match_subtitle_episode)
    _parse_subtitle_result = SearchSubtitleOwner._parse_subtitle_result
    _remove_duplicate_subtitles = staticmethod(SearchSubtitleOwner._remove_duplicate_subtitles)
    _async_search_subtitles_for_media = SearchSubtitleOwner._async_search_subtitles_for_media
    _async_search_subtitles_for_media_stream = SearchSubtitleOwner._async_search_subtitles_for_media_stream
    _SearchChain__search_all_sites = SearchProviderOwner._search_all_sites
    _selected_site_ids = staticmethod(SearchProviderOwner._selected_site_ids)
    _select_indexers = staticmethod(SearchProviderOwner._select_indexers)
    _sync_indexers = SearchProviderOwner._sync_indexers
    _async_indexers = SearchProviderOwner._async_indexers
    _torrent_keyword = staticmethod(SearchProviderOwner._torrent_keyword)
    _torrent_type = staticmethod(SearchProviderOwner._torrent_type)
    _search_site_torrents_with_budget = SearchProviderOwner._search_site_torrents_with_budget
    _iter_provider_batches = SearchProviderOwner._iter_provider_batches
    _iter_provider_events = SearchProviderOwner._iter_provider_events
    _iter_torrent_events = SearchProviderOwner._iter_torrent_events
    _iter_subtitle_events = SearchProviderOwner._iter_subtitle_events
    _iter_site_page_results = SearchPaginationOwner._iter_site_page_results
    _SearchChain__async_search_all_sites = SearchProviderOwner._async_search_all_sites
    _SearchChain__async_search_all_sites_stream = SearchProviderOwner._async_search_all_sites_stream
    _async_search_subtitles_all_sites = SearchProviderOwner._async_search_subtitles_all_sites
    _async_search_subtitles_all_sites_stream = SearchProviderOwner._async_search_subtitles_all_sites_stream

    @eventmanager.register(EventType.SiteDeleted)
    @_public_handler
    def remove_site(self, event: Event) -> None:
        """通过稳定 SearchChain 身份清理已删除站点的搜索配置。"""
        SearchSiteOwner._remove_site(cast(SearchSiteOwner, self), event)
