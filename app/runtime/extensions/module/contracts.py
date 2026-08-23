"""字符串模块方法协议的可检查契约清单。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ModuleResultAggregation(StrEnum):
    """描述多模块结果沿调用链的兼容聚合方式。"""

    LEGACY = "legacy"
    FIRST_NON_EMPTY = "first_non_empty"
    ORDERED_LIST_MERGE = "ordered_list_merge"
    ORDERED_MAPPING_MERGE = "ordered_mapping_merge"
    PIPELINE_RELAY = "pipeline_relay"


class ModuleResultShape(StrEnum):
    """描述模块 provider 返回值的基础 Python 形状。"""

    ANY = "any"
    LIST = "list"
    STRING = "string"
    MAPPING = "mapping"
    BOOLEAN = "boolean"
    BYTES = "bytes"


class ModuleExecutionMode(StrEnum):
    """描述 provider 可以采用的执行形态。"""

    SYNC_OR_ASYNC = "sync_or_async"


class ModuleErrorPolicy(StrEnum):
    """描述单个 provider 失败后的兼容处理策略。"""

    ISOLATE_PROVIDER = "isolate_provider"


class ModuleCapability(Protocol):
    """宿主和新插件可用于声明动态能力的最小 Protocol。"""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """执行模块能力并返回契约声明的结果。"""


@dataclass(frozen=True, slots=True)
class ModuleMethodContract:
    """记录模块方法的输入、结果、执行与兼容错误协议。"""

    family: str
    aggregation: ModuleResultAggregation = ModuleResultAggregation.LEGACY
    version: int = 1
    input_contract: str = "legacy_args"
    result_contract: str = "Any"
    result_shape: ModuleResultShape = ModuleResultShape.ANY
    required_parameters: tuple[str, ...] = ()
    execution: ModuleExecutionMode = ModuleExecutionMode.SYNC_OR_ASYNC
    timeout_policy: str = "caller_budget"
    error_policy: ModuleErrorPolicy = ModuleErrorPolicy.ISOLATE_PROVIDER
    public_to_plugins: bool = True
    supports_sync: bool = True
    supports_async: bool = True
    plugin_short_circuit: bool = True


_DEFAULT_CONTRACT = ModuleMethodContract(family="legacy")

# 首批登记高频能力族。方法名仍保持开放字符串，以兼容第三方插件自定义模块能力；
# 未命中项继续使用冻结的 legacy 规则，并由架构快照记录新增调用位置。
_METHOD_CONTRACTS = {
    "recognize_media": ModuleMethodContract(
        family="media-recognition", input_contract="MediaRecognitionRequest",
        result_contract="MediaInfo | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "mtype", "media_source", "media_id", "episode_group", "cache"),
    ),
    "search_medias": ModuleMethodContract(
        family="media-recognition", input_contract="MediaSearchRequest",
        result_contract="list[MediaInfo]", result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("meta", "media_source"),
    ),
    "obtain_images": ModuleMethodContract(family="media-recognition", input_contract="MediaInfo", result_contract="MediaInfo | None", aggregation=ModuleResultAggregation.PIPELINE_RELAY, required_parameters=("mediainfo",)),
    "media_category": ModuleMethodContract(family="media-recognition", input_contract="MediaCategoryRequest", result_contract="CategoryConfig | None"),
    "mediaserver_items": ModuleMethodContract(family="media-server", input_contract="MediaServerItemsRequest", result_contract="list[MediaServerItem]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("server", "library_id", "start_index", "limit")),
    "mediaserver_iteminfo": ModuleMethodContract(family="media-server", input_contract="MediaServerItemRequest", result_contract="MediaServerItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("server", "item_id")),
    "mediaserver_play_url": ModuleMethodContract(family="media-server", input_contract="MediaServerPlayRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("server", "item_id")),
    "mediaserver_tv_episodes": ModuleMethodContract(family="media-server", input_contract="MediaServerEpisodesRequest", result_contract="list[MediaServerPlayItem]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("server", "item_id")),
    "download_file": ModuleMethodContract(family="storage", input_contract="StorageDownloadRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "path")),
    "upload_file": ModuleMethodContract(family="storage", input_contract="StorageUploadRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "path", "new_name")),
    "list_files": ModuleMethodContract(family="storage", input_contract="StorageListRequest", result_contract="list[FileItem]", result_shape=ModuleResultShape.LIST, aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("fileitem", "recursion")),
    "get_file_item": ModuleMethodContract(family="storage", input_contract="StorageItemRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path")),
    "get_folder": ModuleMethodContract(family="storage", input_contract="StorageFolderRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path")),
    "get_parent_item": ModuleMethodContract(family="storage", input_contract="StorageParentRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem",)),
    "rename_file": ModuleMethodContract(family="storage", input_contract="StorageRenameRequest", result_contract="bool | FileItem", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "name")),
    "storage_manage": ModuleMethodContract(family="storage", input_contract="StorageManageRequest", result_contract="StorageProviderResult", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "action")),
    "snapshot_storage": ModuleMethodContract(family="storage", input_contract="StorageSnapshotRequest", result_contract="dict[str, dict] | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path", "last_snapshot_time", "max_depth", "previous_snapshot")),
    "send_message": ModuleMethodContract(family="messaging", input_contract="MessageSendRequest", result_contract="Message | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY),
    "finalize_message": ModuleMethodContract(family="messaging", input_contract="MessageFinalizeRequest", result_contract="Message | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("response",)),
    "register_commands": ModuleMethodContract(family="messaging", input_contract="CommandRegistrationRequest", result_contract="None", required_parameters=("commands",)),
    "scheduler_job": ModuleMethodContract(family="scheduling", input_contract="SchedulerJobRequest", result_contract="None"),
    "webhook_parser": ModuleMethodContract(family="integration", input_contract="WebhookRequest", result_contract="WebhookEventInfo | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("body", "form", "args")),
    "download_discord_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_feishu_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_feishu_image_to_data_url": ModuleMethodContract(family="messaging", input_contract="MessageImageDownloadRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("image_ref", "source")),
    "download_qq_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_slack_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_slack_file_to_data_url": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_url", "source")),
    "download_synologychat_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_telegram_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_id", "source")),
    "download_telegram_file_to_base64": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_id", "source")),
    "download_vocechat_file_bytes": ModuleMethodContract(family="messaging", input_contract="MessageFileDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("file_ref", "source")),
    "download_vocechat_image_to_data_url": ModuleMethodContract(family="messaging", input_contract="MessageImageDownloadRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("image_ref", "source")),
    "download_wechat_image_to_data_url": ModuleMethodContract(family="messaging", input_contract="MessageImageDownloadRequest", result_contract="str | None", result_shape=ModuleResultShape.STRING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("image_ref", "source")),
    "download_wechat_media_bytes": ModuleMethodContract(family="messaging", input_contract="MessageMediaDownloadRequest", result_contract="bytes | None", result_shape=ModuleResultShape.BYTES, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("media_ref", "source")),
    "downloader_info": ModuleMethodContract(family="downloader", input_contract="DownloaderInfoRequest", result_contract="list[DownloaderInfo]", result_shape=ModuleResultShape.LIST, aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("downloader",)),
    "list_torrents": ModuleMethodContract(family="downloader", input_contract="TorrentListRequest", result_contract="list[DownloaderTorrent]", result_shape=ModuleResultShape.LIST, aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("status", "hashs", "downloader", "include_all_tags")),
    "torrent_files": ModuleMethodContract(family="downloader", input_contract="TorrentFilesRequest", result_contract="DownloaderFileCollection | None", required_parameters=("tid", "downloader")),
    "get_torrent_trackers": ModuleMethodContract(family="downloader", input_contract="TorrentTrackersRequest", result_contract="dict[str, list[str]] | None", result_shape=ModuleResultShape.MAPPING, aggregation=ModuleResultAggregation.ORDERED_MAPPING_MERGE, required_parameters=("hash_string", "downloader")),
    "download": ModuleMethodContract(family="downloader", input_contract="DownloadTaskRequest", result_contract="DownloadTaskResult | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("content", "download_dir", "cookie", "episodes", "category", "label", "downloader")),
    "remove_torrents": ModuleMethodContract(family="downloader", input_contract="TorrentRemoveRequest", result_contract="bool | None", result_shape=ModuleResultShape.BOOLEAN, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("hashs", "delete_file", "downloader")),
    "set_torrents_tag": ModuleMethodContract(family="downloader", input_contract="TorrentTagRequest", result_contract="bool | None", result_shape=ModuleResultShape.BOOLEAN, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("hashs", "tags", "downloader")),
    "start_torrents": ModuleMethodContract(family="downloader", input_contract="TorrentControlRequest", result_contract="bool | None", result_shape=ModuleResultShape.BOOLEAN, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("hashs", "downloader")),
    "stop_torrents": ModuleMethodContract(family="downloader", input_contract="TorrentControlRequest", result_contract="bool | None", result_shape=ModuleResultShape.BOOLEAN, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("hashs", "downloader")),
    "update_torrent": ModuleMethodContract(family="downloader", input_contract="TorrentUpdateRequest", result_contract="dict[str, bool] | None", result_shape=ModuleResultShape.MAPPING, aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("hash_string", "downloader", "download_limit", "upload_limit", "tracker_list", "save_path", "category", "ratio_limit", "seeding_time_limit")),
}

# 同一能力的同步/异步入口共享不可变契约对象，避免参数和聚合语义各自漂移。
_METHOD_CONTRACTS.update({
    "async_recognize_media": _METHOD_CONTRACTS["recognize_media"],
    "async_search_medias": _METHOD_CONTRACTS["search_medias"],
    "async_obtain_images": _METHOD_CONTRACTS["obtain_images"],
})

_PREFIX_CONTRACTS = (
    ("async_tmdb_", ModuleMethodContract(family="tmdb")),
    ("tmdb_", ModuleMethodContract(family="tmdb")),
    ("async_douban_", ModuleMethodContract(family="douban")),
    ("douban_", ModuleMethodContract(family="douban")),
    ("async_bangumi_", ModuleMethodContract(family="bangumi")),
    ("bangumi_", ModuleMethodContract(family="bangumi")),
    ("async_anilist_", ModuleMethodContract(family="anilist")),
    ("anilist_", ModuleMethodContract(family="anilist")),
    ("tvdb_", ModuleMethodContract(family="tvdb")),
    ("music_", ModuleMethodContract(family="music")),
    ("torrent_", ModuleMethodContract(family="downloader")),
)


# 宿主静态扫描到的全部字符串能力名。第三方插件仍可声明未在这里出现的自定义方法，
# 自定义方法继续走开放的 legacy contract；宿主新增调用则必须先进入本清单。
_OBSERVED_HOST_METHODS = (
    'anilist_credits',
    'anilist_discover',
    'anilist_info',
    'anilist_person_credits',
    'anilist_person_detail',
    'anilist_popular_this_season',
    'anilist_recommendations',
    'anilist_trending',
    'any_files',
    'async_anilist_credits',
    'async_anilist_discover',
    'async_anilist_info',
    'async_anilist_person_credits',
    'async_anilist_person_detail',
    'async_anilist_popular_this_season',
    'async_anilist_recommendations',
    'async_anilist_trending',
    'async_bangumi_calendar',
    'async_bangumi_credits',
    'async_bangumi_discover',
    'async_bangumi_info',
    'async_bangumi_person_credits',
    'async_bangumi_person_detail',
    'async_bangumi_recommend',
    'async_douban_discover',
    'async_douban_info',
    'async_douban_movie_credits',
    'async_douban_movie_recommend',
    'async_douban_person_credits',
    'async_douban_person_detail',
    'async_douban_tv_credits',
    'async_douban_tv_recommend',
    'async_identify_music_by_fingerprint',
    'async_match_doubaninfo',
    'async_match_music_album',
    'async_match_tmdbinfo',
    'async_movie_hot',
    'async_movie_showing',
    'async_movie_top250',
    'async_obtain_images',
    'async_recognize_media',
    'async_refresh_torrents',
    'async_search_collections',
    'async_search_medias',
    'async_search_persons',
    'async_search_subtitles',
    'async_search_torrents',
    'async_tmdb_collection',
    'async_tmdb_discover',
    'async_tmdb_episodes',
    'async_tmdb_group_seasons',
    'async_tmdb_info',
    'async_tmdb_movie_credits',
    'async_tmdb_movie_recommend',
    'async_tmdb_movie_similar',
    'async_tmdb_person_credits',
    'async_tmdb_person_detail',
    'async_tmdb_seasons',
    'async_tmdb_trending',
    'async_tmdb_tv_credits',
    'async_tmdb_tv_recommend',
    'async_tmdb_tv_similar',
    'async_tv_animation',
    'async_tv_hot',
    'async_tv_weekly_chinese',
    'async_tv_weekly_global',
    'async_update_recognize_cache',
    'bangumi_calendar',
    'bangumi_credits',
    'bangumi_discover',
    'bangumi_info',
    'bangumi_person_credits',
    'bangumi_person_detail',
    'bangumi_recommend',
    'channel_manage',
    'clear_cache',
    'create_folder',
    'delete_file',
    'delete_message',
    'douban_discover',
    'douban_info',
    'douban_movie_credits',
    'douban_movie_recommend',
    'douban_person_credits',
    'douban_person_detail',
    'douban_tv_credits',
    'douban_tv_recommend',
    'download',
    'download_added',
    'download_discord_file_bytes',
    'download_feishu_file_bytes',
    'download_feishu_image_to_data_url',
    'download_file',
    'download_qq_file_bytes',
    'download_slack_file_bytes',
    'download_slack_file_to_data_url',
    'download_synologychat_file_bytes',
    'download_telegram_file_bytes',
    'download_telegram_file_to_base64',
    'download_vocechat_file_bytes',
    'download_vocechat_image_to_data_url',
    'download_wechat_image_to_data_url',
    'download_wechat_media_bytes',
    'downloader_info',
    'edit_message',
    'filter_torrents',
    'finalize_message',
    'get_file_item',
    'get_folder',
    'get_parent_item',
    'get_search_page_size',
    'get_torrent_trackers',
    'identify_music_by_fingerprint',
    'list_files',
    'list_torrents',
    'load_category_config',
    'mark_message_processing_finished',
    'mark_message_processing_started',
    'match_doubaninfo',
    'match_music_album',
    'match_tmdbinfo',
    'media_category',
    'media_exists',
    'media_files',
    'media_statistic',
    'mediaserver_image_cookies',
    'mediaserver_iteminfo',
    'mediaserver_items',
    'mediaserver_items_count',
    'mediaserver_latest',
    'mediaserver_latest_images',
    'mediaserver_librarys',
    'mediaserver_play_url',
    'mediaserver_playing',
    'mediaserver_season_episode_ids',
    'mediaserver_tv_episodes',
    'message_parser',
    'metadata_img',
    'metadata_nfo',
    'movie_hot',
    'movie_showing',
    'movie_top250',
    'music_album',
    'music_album_related',
    'music_artist',
    'music_artist_albums',
    'music_artist_related',
    'music_cache_clear',
    'music_cache_delete',
    'music_cache_items',
    'music_chart',
    'music_discover',
    'music_fresh_releases',
    'music_lyrics',
    'obtain_images',
    'obtain_specific_image',
    'recognize_media',
    'recommend_name',
    'refresh_torrents',
    'refresh_userdata',
    'register_commands',
    'remove_torrents',
    'rename_file',
    'save_category_config',
    'scheduler_job',
    'search_collections',
    'search_medias',
    'search_music',
    'search_persons',
    'search_subtitles',
    'search_torrents',
    'search_tvdb',
    'send_direct_message',
    'set_torrents_tag',
    'site_subtitle_links',
    'snapshot_storage',
    'start_torrents',
    'stop_torrents',
    'storage_manage',
    'tmdb_cache_clear',
    'tmdb_cache_delete',
    'tmdb_cache_items',
    'tmdb_collection',
    'tmdb_discover',
    'tmdb_episodes',
    'tmdb_group_seasons',
    'tmdb_info',
    'tmdb_movie_credits',
    'tmdb_movie_recommend',
    'tmdb_movie_similar',
    'tmdb_person_credits',
    'tmdb_person_detail',
    'tmdb_seasons',
    'tmdb_trending',
    'tmdb_tv_credits',
    'tmdb_tv_recommend',
    'tmdb_tv_similar',
    'torrent_files',
    'transfer',
    'transfer_completed',
    'tv_animation',
    'tv_hot',
    'tv_weekly_chinese',
    'tv_weekly_global',
    'tvdb_info',
    'tvdb_slug',
    'update_recognize_cache',
    'update_torrent',
    'upload_file',
    'user_authenticate',
    'webhook_parser',
)

_FAMILY_IO_CONTRACTS = {
    "anilist": ("AniListKeywordArguments", "AniListProviderResult"),
    "authentication": ("AuthenticationKeywordArguments", "AuthenticationResult"),
    "bangumi": ("BangumiKeywordArguments", "BangumiProviderResult"),
    "category": ("CategoryKeywordArguments", "CategoryProviderResult"),
    "douban": ("DoubanKeywordArguments", "DoubanProviderResult"),
    "downloader": ("DownloaderKeywordArguments", "DownloaderProviderResult"),
    "integration": ("IntegrationKeywordArguments", "IntegrationProviderResult"),
    "media-discovery": ("MediaDiscoveryKeywordArguments", "MediaDiscoveryProviderResult"),
    "media-recognition": ("MediaRecognitionKeywordArguments", "MediaRecognitionProviderResult"),
    "media-server": ("MediaServerKeywordArguments", "MediaServerProviderResult"),
    "messaging": ("MessagingKeywordArguments", "MessagingProviderResult"),
    "metadata": ("MetadataKeywordArguments", "MetadataProviderResult"),
    "music": ("MusicKeywordArguments", "MusicProviderResult"),
    "site": ("SiteKeywordArguments", "SiteProviderResult"),
    "storage": ("StorageKeywordArguments", "StorageProviderResult"),
    "tmdb": ("TmdbKeywordArguments", "TmdbProviderResult"),
    "tvdb": ("TvdbKeywordArguments", "TvdbProviderResult"),
}


def _infer_observed_family(method: str) -> str:
    """按稳定能力前缀把已观察宿主方法归入可审计的输入/结果族。"""
    for prefix, contract in _PREFIX_CONTRACTS:
        if method.startswith(prefix):
            return contract.family
    if method.startswith(("mediaserver_", "media_exists", "media_statistic")):
        return "media-server"
    if method.startswith((
        "download", "torrent_", "list_torrents", "refresh_torrents",
        "remove_torrents", "start_torrents", "stop_torrents",
        "set_torrents_tag", "update_torrent", "get_torrent_trackers",
        "downloader_info", "filter_torrents", "transfer_completed",
    )):
        return "downloader"
    if method.startswith((
        "channel_", "delete_message", "edit_message", "finalize_message",
        "mark_message_", "message_parser", "register_commands",
        "send_direct_message", "send_message",
    )):
        return "messaging"
    if method.startswith((
        "any_files", "create_folder", "delete_file", "get_file_item",
        "get_folder", "get_parent_item", "list_files", "media_files",
        "rename_file", "snapshot_storage", "storage_manage", "transfer",
        "upload_file",
    )):
        return "storage"
    if method.startswith((
        "metadata_", "obtain_specific_image", "recommend_name",
    )):
        return "metadata"
    if method.startswith((
        "async_identify_music", "async_match_music", "identify_music",
        "match_music", "search_music",
    )):
        return "music"
    if method.startswith((
        "async_match_", "async_obtain_images", "async_recognize_media",
        "async_update_recognize_cache", "match_", "obtain_images",
        "recognize_media", "update_recognize_cache",
    )):
        return "media-recognition"
    if method.startswith((
        "async_movie_", "async_search_", "async_tv_", "movie_",
        "search_collections", "search_medias", "search_persons",
        "search_subtitles", "search_torrents", "tv_",
    )):
        return "media-discovery"
    if method in {"clear_cache", "load_category_config", "save_category_config"}:
        return "category"
    if method in {"get_search_page_size", "refresh_userdata", "site_subtitle_links"}:
        return "site"
    if method == "user_authenticate":
        return "authentication"
    return "integration"


def _register_observed_host_contracts() -> None:
    """为全部宿主字符串调用登记完整 V2 字段，保留未知插件方法的 legacy fallback。"""
    for method in _OBSERVED_HOST_METHODS:
        if method in _METHOD_CONTRACTS:
            continue
        family = _infer_observed_family(method)
        input_contract, result_contract = _FAMILY_IO_CONTRACTS[family]
        _METHOD_CONTRACTS[method] = ModuleMethodContract(
            family=family,
            input_contract=input_contract,
            result_contract=result_contract,
        )


_register_observed_host_contracts()



def get_module_method_contract(method: str) -> ModuleMethodContract:
    """返回方法的显式能力族契约，未知方法保持既有 legacy 协议。"""
    if contract := _METHOD_CONTRACTS.get(method):
        return contract
    for prefix, contract in _PREFIX_CONTRACTS:
        if method.startswith(prefix):
            return contract
    return _DEFAULT_CONTRACT


def is_explicit_module_method(method: str) -> bool:
    """判断方法是否已进入首批显式能力族清单。"""
    return get_module_method_contract(method) is not _DEFAULT_CONTRACT


def diagnose_module_callable(method: str, callback: Callable[..., Any]) -> tuple[str, ...]:
    """诊断显式能力的基础签名；兼容阶段只返回问题，不拒绝 provider。"""
    contract = get_module_method_contract(method)
    if contract is _DEFAULT_CONTRACT:
        return ()
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return ("signature-unavailable",)
    missing = tuple(
        name
        for name in contract.required_parameters
        if name not in parameters
        and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    )
    return tuple(f"missing-parameter:{name}" for name in missing)


def diagnose_module_result(method: str, result: Any) -> tuple[str, ...]:
    """诊断显式模块结果的基础形状，兼容阶段只告警而不改写返回值。"""
    shape = get_module_method_contract(method).result_shape
    if shape is ModuleResultShape.ANY or result is None:
        return ()
    matches = {
        ModuleResultShape.LIST: isinstance(result, list),
        ModuleResultShape.STRING: isinstance(result, str),
        ModuleResultShape.MAPPING: isinstance(result, dict),
        ModuleResultShape.BOOLEAN: isinstance(result, bool),
        ModuleResultShape.BYTES: isinstance(result, bytes),
    }
    if matches.get(shape, True):
        return ()
    return (f"unexpected-result:{shape.value}:{type(result).__name__}",)


def list_explicit_module_contracts() -> dict[str, ModuleMethodContract]:
    """返回显式方法清单的副本，供架构基线和 SDK 文档使用。"""
    return dict(_METHOD_CONTRACTS)
