"""DownloadChain owner 的静态组合合同。"""

from typing import TYPE_CHECKING, Any, Callable

from app.chain.base import ChainBase

if TYPE_CHECKING:
    class _DownloadOwnerBase(ChainBase):
        """向类型检查器声明同一 Facade 上的跨 owner 方法。"""

        _active_download_failure_fingerprints: Callable[..., Any]
        _append_no_exists: Callable[..., None]
        _build_download_notification: Callable[..., Any]
        _build_download_failure_fingerprint: Callable[..., Any]
        _build_download_note: Callable[..., dict[str, Any]]
        _download_failure_ttl: Callable[..., int]
        _download_file_deleted: Callable[..., None]
        _download_movie_music_candidates: Callable[..., None]
        _execute_batch_download: Callable[..., Any]
        _execute_download_single: Callable[..., Any]
        _format_failure_episodes: Callable[..., Any]
        _is_job_active: Callable[..., bool]
        _is_subscribe_source: Callable[..., bool]
        _log_download_failure_cooldown: Callable[..., None]
        _media_identity_keys: Callable[..., set[str]]
        _matches_media_identity: Callable[..., bool]
        _prepare_batch_download_contexts: Callable[..., Any]
        _record_download_failure: Callable[..., Any]
        _resolve_media_download_dir: Callable[..., Any]
        _settle_download_success: Callable[..., None]
        _submit_download_added_task: Callable[..., None]
        _subscription_download_cancelled: Callable[..., bool]
        _validate_music_album_resource: Callable[..., Any]
        batch_download: Callable[..., Any]
        download_single: Callable[..., Any]
        download_torrent: Callable[..., Any]
        download_site_subtitles: Callable[..., None]
        get_no_exists_info: Callable[..., Any]
else:
    _DownloadOwnerBase = ChainBase
