"""订阅职责 owner 的静态组合宿主合同。"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Optional, Union

    from app.domain.context import MediaInfo, MusicInfo, TorrentInfo
    from app.domain.meta.metabase import MetaBase
    from app.schemas.message import Message
    from app.schemas.transfer import TransferInfo

    class _SubscribeOwnerHost:
        """声明 SubscribeChain 组合后向各 owner 提供的属性和兄弟职责。"""

        _SUBSCRIPTION_EXECUTION_TTL: int
        _match_lock: Any
        _search_queue_lock: Any
        _subscription_execution_admission: Any
        download_history_repository: Any
        eventmanager: Any
        messagehelper: Any
        rule_group_mutation_scope: Callable[..., Any]
        runtime_config: Any
        site_reference_mutation_scope: Callable[..., Any]
        site_repository: Any
        subscription_completion_scope: Callable[..., Any]
        subscription_repository: Any
        subscription_search_repository: Any
        sync_subscription_delete_scope: Callable[..., Any]
        sync_subscription_mutation_scope: Callable[..., Any]

        _SubscribeChain__apply_episodes_refresh: Callable[..., Any]
        _SubscribeChain__apply_subscribe_update: Callable[..., Any]
        _SubscribeChain__async_apply_episodes_refresh: Callable[..., Awaitable[Any]]
        _SubscribeChain__async_notify_subscribe_create_failure: Callable[..., Awaitable[Any]]
        _SubscribeChain__async_post_subscribe_added: Callable[..., Awaitable[Any]]
        _SubscribeChain__build_completion_notification: Callable[..., Any]
        _SubscribeChain__build_subscribe_notification: Callable[..., Any]
        _SubscribeChain__download_best_version_with_full_pack_first: Callable[..., Any]
        _SubscribeChain__get_best_version_target_episodes: Callable[..., Any]
        _SubscribeChain__get_default_subscribe_config: Callable[..., Any]
        _SubscribeChain__get_downloaded: Callable[..., Any]
        _SubscribeChain__get_downloaded_best_version_episodes: Callable[..., Any]
        _SubscribeChain__get_episode_priority: Callable[..., Any]
        _SubscribeChain__get_subscribe_no_exits: Callable[..., Any]
        _SubscribeChain__is_best_version_complete: Callable[..., Any]
        _SubscribeChain__is_full_best_version_enabled: Callable[..., Any]
        _SubscribeChain__is_full_season_best_version_resource: Callable[..., Any]
        _SubscribeChain__notify_subscribe_create_failure: Callable[..., Any]
        _SubscribeChain__post_subscribe_added: Callable[..., Any]
        _SubscribeChain__prepare_best_version_tv_candidate: Callable[..., Any]
        _SubscribeChain__prepare_subscribe_progress_fields: Callable[..., Any]
        _SubscribeChain__prepare_total_episode_change_fields: Callable[..., Any]
        _SubscribeChain__refresh_subscribe_progress_with_no_exists: Callable[..., Any]
        _SubscribeChain__refresh_total_episode_before_completion: Callable[..., Any]
        _SubscribeChain__report_completed: Callable[..., Any]
        _SubscribeChain__resolve_total_episode_decrease: Callable[..., Any]
        _acquire_run_lock: Callable[..., Any]
        _async_recognize_music_subscribe: Callable[..., Awaitable[Any]]
        _execute_search_task: Callable[..., Any]
        _get_pending_best_version_episodes: Callable[..., Any]
        _is_episode_range_covered: Callable[..., Any]
        _is_music_download_complete: Callable[..., Any]
        _load_search_subscriptions: Callable[..., Any]
        _match_music_subscribe: Callable[..., Any]
        _notify_manual_search: Callable[..., Any]
        _process_search_subscription: Callable[..., Any]
        _recognize_music_subscribe: Callable[..., Any]
        _report_search_progress: Callable[..., Any]
        _search_music_subscribe: Callable[..., Any]
        _subscription_query: Callable[..., Any]
        _defer_recent_subscription: Callable[..., Any]
        _validate_music_subscribe_target: Callable[..., Any]
        _wait_before_scheduled_search: Callable[..., Any]
        add: Callable[..., Any]
        async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
            """异步补全媒体图片。"""
            raise NotImplementedError

        async def async_post_message(
            self,
            message: Optional[Message] = None,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            **kwargs: Any,
        ) -> None:
            """异步发送订阅通知。"""
            raise NotImplementedError
        check_and_handle_existing_media: Callable[..., Any]
        check_and_reconcile: Callable[..., Any]
        filter_torrents: Callable[..., Any]
        finish_subscribe_or_not: Callable[..., Any]
        get_params: Callable[..., Any]
        get_states_for_search: Callable[..., Any]
        get_sub_sites: Callable[..., Any]
        get_subscribe_source_keyword: Callable[..., Any]
        get_subscribed_sites: Callable[..., Any]
        has_music_subscribe: Callable[..., Any]
        match: Callable[..., Any]
        media_exists: Callable[..., Any]
        media_files: Callable[..., Any]
        obtain_images: Callable[..., Any]
        parse_subscribe_source_keyword: Callable[..., Any]
        post_message: Callable[..., Any]
        remote_list: Callable[..., Any]
        resolve_subscribe_missing: Callable[..., Any]
        reconcile_subscription_completion: Callable[..., Any]
        resume_search_queue: Callable[..., Any]

    _SubscribeOwnerBase = _SubscribeOwnerHost
else:
    _SubscribeOwnerBase = object
