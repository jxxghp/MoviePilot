"""搜索处理链稳定 Facade。"""

from collections.abc import Callable
from typing import Any, TypeVar, cast

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
from app.runtime.events import Event, eventmanager
from app.schemas.types import EventType

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
    is_ai_recommend_enabled = SearchRecommendOwner.is_ai_recommend_enabled
    _calculate_recommend_request_hash = staticmethod(SearchRecommendOwner._calculate_recommend_request_hash)
    _build_ai_recommend_status = SearchRecommendOwner._build_ai_recommend_status
    get_current_recommend_status_only = SearchRecommendOwner.get_current_recommend_status_only
    get_recommend_status = SearchRecommendOwner.get_recommend_status
    cancel_ai_recommend = SearchRecommendOwner.cancel_ai_recommend
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
    last_search_params = SearchCacheOwner.last_search_params
    async_last_search_params = SearchCacheOwner.async_last_search_params
    _normalize_ai_indices = staticmethod(SearchRecommendOwner._normalize_ai_indices)
    _extract_recommend_items = staticmethod(SearchRecommendOwner._extract_recommend_items)
    _restore_original_indices = staticmethod(SearchRecommendOwner._restore_original_indices)
    _invoke_recommend_llm = staticmethod(SearchRecommendOwner._invoke_recommend_llm)
    start_recommend_task = SearchRecommendOwner.start_recommend_task
    search_by_id = SearchMediaOwner.search_by_id
    search_by_title = SearchTitleOwner.search_by_title
    last_search_results = SearchCacheOwner.last_search_results
    async_last_search_results = SearchCacheOwner.async_last_search_results
    async_last_subtitle_search_results = SearchCacheOwner.async_last_subtitle_search_results
    async_search_subtitles_by_title = SearchSubtitleOwner.async_search_subtitles_by_title
    async_search_subtitles_by_title_stream = SearchSubtitleOwner.async_search_subtitles_by_title_stream
    async_search_subtitles_by_id = SearchSubtitleOwner.async_search_subtitles_by_id
    async_search_subtitles_by_id_stream = SearchSubtitleOwner.async_search_subtitles_by_id_stream
    async_search_by_id = SearchMediaOwner.async_search_by_id
    async_search_by_title = SearchTitleOwner.async_search_by_title
    async_search_by_title_stream = SearchTitleOwner.async_search_by_title_stream
    _build_title_search_meta = staticmethod(SearchTitleOwner._build_title_search_meta)
    _filter_title_search_torrents = SearchTitleOwner._filter_title_search_torrents
    async_search_by_id_stream = SearchMediaOwner.async_search_by_id_stream
    _prepare_params = staticmethod(SearchPlanOwner._prepare_params)
    _copy_media_input = staticmethod(SearchPlanOwner._copy_media_input)
    _parse_result = SearchResultOwner._parse_result
    _remove_duplicate = staticmethod(SearchResultOwner._remove_duplicate)
    _build_music_contexts = SearchMusicOwner._build_music_contexts
    _matching_music_torrents = staticmethod(SearchMusicOwner._matching_music_torrents)
    _process_music = SearchMusicOwner._process_music
    _async_process_music = SearchMusicOwner._async_process_music
    _async_process_music_stream = SearchMusicOwner._async_process_music_stream
    process = SearchMediaOwner.process
    async_process = SearchMediaOwner.async_process
    async_process_stream = SearchMediaOwner.async_process_stream
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
