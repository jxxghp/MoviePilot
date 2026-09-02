"""整理职责 owner 的静态组合宿主合同。"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    class _TransferOwnerHost:
        """声明 TransferChain 组合后向各 owner 提供的属性和兄弟职责。"""

        _LEASE_HEARTBEAT_INTERVAL_SECONDS: float
        _QUEUE_STOP_SENTINEL: object
        _RECOVERY_CLAIM_LIMIT: int
        _RECOVERY_POLL_INTERVAL_SECONDS: float
        _WORKER_LEASE_SECONDS: float
        _allowed_exts: Any
        _audio_exts: Any
        _media_exts: Any
        _module_dispatcher: Any
        _scrape_batches: Any
        _subtitle_exts: Any
        _success_target_files: Any
        _transfer_admissions: Any
        _owned_leases: Any
        _worker_owner_id: str
        _worker_state_lock: Any
        download_history_repository: Any
        eventmanager: Any
        failure_notification_aggregator: Any
        jobview: Any
        runtime_config: Any
        transfer_admission_repository: Any
        transfer_execution_repository: Any
        transfer_history_repository: Any

        _TransferChain__assert_owned_lease: Callable[..., Any]
        _TransferChain__admit_transfer: Callable[..., Any]
        _TransferChain__bind_claimed_admission: Callable[..., Any]
        _TransferChain__build_planning_input: Callable[..., Any]
        _TransferChain__checkpoint_planning_rejection: Callable[..., Any]
        _TransferChain__claim_task_for_execution: Callable[..., Any]
        _TransferChain__default_callback: Callable[..., Any]
        _TransferChain__ensure_lease_heartbeat_owner: Callable[..., Any]
        _TransferChain__ensure_lease_runtime_state: Callable[..., Any]
        _TransferChain__fail_transfer_task: Callable[..., Any]
        _TransferChain__finish_job_execution: Callable[..., Any]
        _TransferChain__forget_owned_lease: Callable[..., Any]
        _TransferChain__get_transfer_target_dir_path: Callable[..., Any]
        _TransferChain__handle_planned_transfer: Callable[..., Any]
        _TransferChain__handle_transfer: Callable[..., Any]
        _TransferChain__json_snapshot: Callable[..., Any]
        _TransferChain__mark_torrent_completed_if_done: Callable[..., Any]
        _TransferChain__put_to_jobview: Callable[..., Any]
        _TransferChain__record_uncheckpointed_failure: Callable[..., Any]
        _TransferChain__register_claimed_admission: Callable[..., Any]
        _TransferChain__release_admission_claim: Callable[..., Any]
        _TransferChain__release_task_claim: Callable[..., Any]
        _TransferChain__restore_mediainfo_snapshot: Callable[..., Any]
        _TransferChain__restore_meta_snapshot: Callable[..., Any]
        _TransferChain__restore_planned_task: Callable[..., Any]
        _TransferChain__select_storage_oper: Callable[..., Any]
        _TransferChain__settle_legacy_transfer_result: Callable[..., Any]
        _TransferChain__start_job_execution: Callable[..., Any]
        _TransferChain__transfer_plan_fingerprint: Callable[..., Any]
        _build_transfer_fileitem: Callable[..., Any]
        _can_delete_torrent: Callable[..., Any]
        _close_scrape_batch: Callable[..., Any]
        _delete_manual_transfer_history: Callable[..., Any]
        _download_history_music_type: Callable[..., Any]
        _execute_transfer: Callable[..., Any]
        _finish_scrape_batch_task: Callable[..., Any]
        _get_file_key: Callable[..., Any]
        _get_manual_transfer_history: Callable[..., Any]
        _get_shared_download_roots: Callable[..., Any]
        _get_subscribe_custom_words: Callable[..., Any]
        _is_allow_filesize: Callable[..., Any]
        _is_audio_file: Callable[..., Any]
        _is_blocked_by_exclude_words: Callable[..., Any]
        _is_hidden_or_recycle_path: Callable[..., Any]
        _is_media_file: Callable[..., Any]
        _is_movie_year_conflict: Callable[..., Any]
        _is_music_lyrics_file: Callable[..., Any]
        _is_music_retry_source: Callable[..., Any]
        _is_overwrite_declined: Callable[..., Any]
        _is_primary_media_file: Callable[..., Any]
        _is_subtitle_file: Callable[..., Any]
        _match_music_album_context: Callable[..., Any]
        _music_info_from_meta: Callable[..., Any]
        _plan_checkpoint_and_execute: Callable[..., Any]
        _re_transfer: Callable[..., Any]
        _recognize_music_retry_media: Callable[..., Any]
        _record_scrape_target: Callable[..., Any]
        _register_scrape_batch_task: Callable[..., Any]
        _request_durable_transfer_retry: Callable[..., Any]
        _requires_automatic_category: Callable[..., Any]
        _resolve_download_history: Callable[..., Any]
        _restore_music_download_context: Callable[..., Any]
        _send_metadata_scrape_event: Callable[..., Any]
        _should_delete_empty_source_directories: Callable[..., Any]
        _transfer_storage_chain: Callable[..., Any]
        _transfer_media_chain: Callable[..., Any]
        _transfer_subscribe_chain: Callable[..., Any]
        build_failed_transfer_buttons: Callable[..., Any]
        do_transfer: Callable[..., Any]
        execute_transfer_plan: Callable[..., Any]
        list_torrents: Callable[..., Any]
        obtain_images: Callable[..., Any]
        plan_transfer: Callable[..., Any]
        post_message: Callable[..., Any]
        put_to_queue: Callable[..., Any]
        redo_transfer_history: Callable[..., Any]
        remove_torrents: Callable[..., Any]
        run_module: Callable[..., Any]
        send_transfer_message: Callable[..., Any]
        torrent_files: Callable[..., Any]
        transfer_completed: Callable[..., Any]

        async_post_message: Callable[..., Any]

        def __init__(self, runtime_context: Any | None = None) -> None:
            """声明最终 Chain 提供的运行上下文构造入口。"""
            return None

    _TransferOwnerBase = _TransferOwnerHost
else:
    _TransferOwnerBase = object
