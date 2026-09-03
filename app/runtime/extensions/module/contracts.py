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
    FAN_OUT = "fan_out"


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
        family="media-recognition",
        input_contract="MediaRecognitionRequest",
        result_contract="MediaInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "mtype", "media_source", "media_id", "episode_group", "cache"),
    ),
    "match_doubaninfo": ModuleMethodContract(
        family="media-recognition",
        input_contract="DoubanMatchRequest",
        result_contract="dict[str, Any] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("name", "imdbid", "mtype", "year", "season", "raise_exception"),
    ),
    "match_tmdbinfo": ModuleMethodContract(
        family="media-recognition",
        input_contract="TmdbMatchRequest",
        result_contract="dict[str, Any] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("name", "mtype", "year", "season"),
    ),
    "update_recognize_cache": ModuleMethodContract(
        family="media-recognition",
        input_contract="RecognitionCacheUpdateRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "mediainfo"),
    ),
    "search_medias": ModuleMethodContract(
        family="media-recognition",
        input_contract="MediaSearchRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("meta", "media_source"),
    ),
    "get_media_auxiliary_info": ModuleMethodContract(
        family="media-recognition",
        input_contract="MediaAuxiliaryInfoRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("mediainfo", "media_source", "metainfo"),
        plugin_short_circuit=False,
    ),
    "get_media_classification_facts": ModuleMethodContract(
        family="media-classification",
        version=1,
        input_contract="ClassificationEnrichmentRequest",
        result_contract="ClassificationEnrichmentResponse | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FAN_OUT,
        required_parameters=("request",),
        supports_async=False,
        plugin_short_circuit=False,
    ),
    "obtain_images": ModuleMethodContract(
        family="media-recognition",
        input_contract="MediaInfo",
        result_contract="MediaInfo | None",
        aggregation=ModuleResultAggregation.PIPELINE_RELAY,
        required_parameters=("mediainfo",),
    ),
    "mediaserver_items": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerItemsRequest",
        result_contract="Iterable[MediaServerItem] | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "library_id", "start_index", "limit"),
    ),
    "mediaserver_iteminfo": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerItemRequest",
        result_contract="MediaServerItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "item_id"),
    ),
    "mediaserver_play_url": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerPlayRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "item_id"),
    ),
    "mediaserver_tv_episodes": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerEpisodesRequest",
        result_contract="list[MediaServerPlayItem]",
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server", "item_id"),
    ),
    "media_exists": ModuleMethodContract(
        family="media-server",
        input_contract="MediaExistsRequest",
        result_contract="ExistMediaInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("mediainfo", "itemid", "server"),
    ),
    "media_statistic": ModuleMethodContract(
        family="media-server",
        input_contract="MediaStatisticRequest",
        result_contract="list[Statistic]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server",),
    ),
    "mediaserver_image_cookies": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerImageCookiesRequest",
        result_contract="str | dict | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "image_url"),
    ),
    "mediaserver_items_count": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerItemsCountRequest",
        result_contract="int | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "library_id"),
    ),
    "mediaserver_latest": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerRecentRequest",
        result_contract="list[MediaServerPlayItem]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server", "count", "username"),
    ),
    "mediaserver_latest_images": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerRecentImagesRequest",
        result_contract="list[str]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server", "count", "remote", "username"),
    ),
    "mediaserver_librarys": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerLibrariesRequest",
        result_contract="list[MediaServerLibrary]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server", "username", "hidden"),
    ),
    "mediaserver_playing": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerRecentRequest",
        result_contract="list[MediaServerPlayItem]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("server", "count", "username"),
    ),
    "mediaserver_season_episode_ids": ModuleMethodContract(
        family="media-server",
        input_contract="MediaServerSeasonEpisodesRequest",
        result_contract="dict[int, str] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("server", "item_id", "season"),
    ),
    "any_files": ModuleMethodContract(
        family="storage",
        input_contract="StorageAnyFilesRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem", "extensions"),
    ),
    "create_folder": ModuleMethodContract(
        family="storage",
        input_contract="StorageCreateFolderRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem", "name"),
    ),
    "delete_file": ModuleMethodContract(
        family="storage",
        input_contract="StorageDeleteRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem",),
    ),
    "download_file": ModuleMethodContract(
        family="storage",
        input_contract="StorageDownloadRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem", "path"),
    ),
    "upload_file": ModuleMethodContract(
        family="storage",
        input_contract="StorageUploadRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem", "path", "new_name"),
    ),
    "list_files": ModuleMethodContract(
        family="storage",
        input_contract="StorageListRequest",
        result_contract="list[FileItem]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("fileitem", "recursion"),
    ),
    "media_files": ModuleMethodContract(
        family="storage",
        input_contract="MediaFilesRequest",
        result_contract="list[FileItem]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("mediainfo",),
    ),
    "get_file_item": ModuleMethodContract(
        family="storage",
        input_contract="StorageItemRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("storage", "path"),
    ),
    "get_folder": ModuleMethodContract(
        family="storage",
        input_contract="StorageFolderRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("storage", "path"),
    ),
    "get_parent_item": ModuleMethodContract(
        family="storage",
        input_contract="StorageParentRequest",
        result_contract="FileItem | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem",),
    ),
    "rename_file": ModuleMethodContract(
        family="storage",
        input_contract="StorageRenameRequest",
        result_contract="bool | FileItem",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("fileitem", "name"),
    ),
    "storage_manage": ModuleMethodContract(
        family="storage",
        input_contract="StorageManageRequest",
        result_contract="StorageProviderResult",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("storage", "action"),
    ),
    "snapshot_storage": ModuleMethodContract(
        family="storage",
        input_contract="StorageSnapshotRequest",
        result_contract="dict[str, dict] | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("storage", "path", "last_snapshot_time", "max_depth", "previous_snapshot"),
    ),
    "plan_transfer": ModuleMethodContract(
        family="storage",
        input_contract="TransferPlanningInput",
        result_contract="TransferPlanCheckpoint | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=(
            "fileitem",
            "meta",
            "mediainfo",
            "target_directory",
            "target_storage",
            "target_path",
            "transfer_type",
            "scrape",
            "library_type_folder",
            "library_category_folder",
            "episodes_info",
            "source_oper",
            "preview",
            "planning_input",
        ),
        public_to_plugins=False,
    ),
    "execute_transfer_plan": ModuleMethodContract(
        family="storage",
        input_contract="TransferPlanCheckpoint",
        result_contract="TransferInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=(
            "checkpoint",
            "meta",
            "mediainfo",
            "source_oper",
            "target_oper",
        ),
        public_to_plugins=False,
    ),
    "transfer": ModuleMethodContract(
        family="storage",
        input_contract="TransferRequest",
        result_contract="TransferInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=(
            "fileitem",
            "meta",
            "mediainfo",
            "target_directory",
            "target_storage",
            "target_path",
            "transfer_type",
            "scrape",
            "library_type_folder",
            "library_category_folder",
            "episodes_info",
            "source_oper",
            "target_oper",
            "preview",
        ),
    ),
    "clear_cache": ModuleMethodContract(
        family="category",
        input_contract="CacheClearRequest",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        plugin_short_circuit=False,
    ),
    "get_search_page_size": ModuleMethodContract(
        family="site",
        input_contract="SiteSearchPageSizeRequest",
        result_contract="int | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("site", "keyword"),
    ),
    "refresh_userdata": ModuleMethodContract(
        family="site",
        input_contract="SiteUserDataRequest",
        result_contract="SiteUserData | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("site",),
    ),
    "site_subtitle_links": ModuleMethodContract(
        family="site",
        input_contract="SiteSubtitleLinksRequest",
        result_contract="list[str]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("context",),
    ),
    "metadata_img": ModuleMethodContract(
        family="metadata",
        input_contract="MetadataImageRequest",
        result_contract="dict[str, str] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("mediainfo", "season", "episode"),
    ),
    "metadata_nfo": ModuleMethodContract(
        family="metadata",
        input_contract="MetadataNfoRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "mediainfo", "season", "episode"),
    ),
    "obtain_specific_image": ModuleMethodContract(
        family="metadata",
        input_contract="SpecificImageRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("mediaid", "mtype", "image_type", "image_prefix", "season", "episode"),
    ),
    "recommend_name": ModuleMethodContract(
        family="metadata",
        input_contract="RecommendNameRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "mediainfo", "episodes_info"),
    ),
    "user_authenticate": ModuleMethodContract(
        family="authentication",
        input_contract="UserAuthenticationRequest",
        result_contract="AuthCredentials | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("credentials",),
    ),
    "tvdb_info": ModuleMethodContract(
        family="tvdb",
        input_contract="TvdbInfoRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("tvdbid",),
    ),
    "tvdb_slug": ModuleMethodContract(
        family="tvdb",
        input_contract="TvdbInfoRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("tvdbid",),
    ),
    "search_tvdb": ModuleMethodContract(
        family="tvdb",
        input_contract="TvdbSearchRequest",
        result_contract="list[dict]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("title",),
    ),
    "movie_hot": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "movie_showing": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "movie_top250": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "search_collections": ModuleMethodContract(
        family="media-discovery",
        input_contract="CollectionSearchRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("name", "media_source"),
    ),
    "search_persons": ModuleMethodContract(
        family="media-discovery",
        input_contract="PersonSearchRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("name", "media_source"),
    ),
    "search_subtitles": ModuleMethodContract(
        family="media-discovery",
        input_contract="SubtitleSearchRequest",
        result_contract="list[SubtitleInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("site", "keyword", "page"),
    ),
    "search_torrents": ModuleMethodContract(
        family="media-discovery",
        input_contract="TorrentSearchRequest",
        result_contract="list[TorrentInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("site", "keyword", "mtype", "page"),
    ),
    "tv_animation": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "tv_hot": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "tv_weekly_chinese": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "tv_weekly_global": ModuleMethodContract(
        family="media-discovery",
        input_contract="MediaRankingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "douban_discover": ModuleMethodContract(
        family="douban",
        input_contract="DoubanDiscoverRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("mtype", "sort", "tags", "page", "count"),
    ),
    "douban_info": ModuleMethodContract(
        family="douban",
        input_contract="DoubanInfoRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("doubanid", "mtype", "raise_exception"),
    ),
    "douban_movie_credits": ModuleMethodContract(
        family="douban",
        input_contract="DoubanMediaRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("doubanid",),
    ),
    "douban_movie_recommend": ModuleMethodContract(
        family="douban",
        input_contract="DoubanMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("doubanid",),
    ),
    "douban_person_credits": ModuleMethodContract(
        family="douban",
        input_contract="DoubanPersonCreditsRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("person_id", "page"),
    ),
    "douban_person_detail": ModuleMethodContract(
        family="douban",
        input_contract="DoubanPersonRequest",
        result_contract="MediaPerson | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("person_id",),
    ),
    "douban_tv_credits": ModuleMethodContract(
        family="douban",
        input_contract="DoubanMediaRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("doubanid",),
    ),
    "douban_tv_recommend": ModuleMethodContract(
        family="douban",
        input_contract="DoubanMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("doubanid",),
    ),
    "bangumi_calendar": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiCalendarRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
    ),
    "bangumi_credits": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiMediaRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("bangumiid",),
    ),
    "bangumi_discover": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiDiscoverArguments",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
    ),
    "bangumi_info": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiMediaRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("bangumiid",),
    ),
    "bangumi_person_credits": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiPersonRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("person_id",),
    ),
    "bangumi_person_detail": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiPersonRequest",
        result_contract="MediaPerson | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("person_id",),
    ),
    "bangumi_recommend": ModuleMethodContract(
        family="bangumi",
        input_contract="BangumiMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("bangumiid",),
    ),
    "anilist_credits": ModuleMethodContract(
        family="anilist",
        input_contract="AniListMediaPageRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("anilist_id", "page", "count"),
    ),
    "anilist_discover": ModuleMethodContract(
        family="anilist",
        input_contract="AniListDiscoverArguments",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
    ),
    "anilist_info": ModuleMethodContract(
        family="anilist",
        input_contract="AniListMediaRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("anilist_id",),
    ),
    "anilist_person_credits": ModuleMethodContract(
        family="anilist",
        input_contract="AniListPersonPageRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("person_id", "page", "count"),
    ),
    "anilist_person_detail": ModuleMethodContract(
        family="anilist",
        input_contract="AniListPersonRequest",
        result_contract="MediaPerson | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("person_id",),
    ),
    "anilist_popular_this_season": ModuleMethodContract(
        family="anilist",
        input_contract="AniListPageRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "anilist_recommendations": ModuleMethodContract(
        family="anilist",
        input_contract="AniListMediaPageRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("anilist_id", "page", "count"),
    ),
    "anilist_trending": ModuleMethodContract(
        family="anilist",
        input_contract="AniListPageRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page", "count"),
    ),
    "tmdb_collection": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbCollectionRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("collection_id",),
    ),
    "tmdb_discover": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbDiscoverRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=(
            "mtype",
            "sort_by",
            "with_genres",
            "with_original_language",
            "with_keywords",
            "with_watch_providers",
            "vote_average",
            "vote_count",
            "release_date",
            "page",
        ),
    ),
    "tmdb_episodes": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbEpisodesRequest",
        result_contract="list[TmdbEpisode]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid", "season", "episode_group"),
    ),
    "tmdb_group_seasons": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbGroupRequest",
        result_contract="list[TmdbSeason]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("group_id",),
    ),
    "tmdb_info": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbInfoRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("tmdbid", "mtype", "season"),
    ),
    "tmdb_movie_credits": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaPageRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid", "page"),
    ),
    "tmdb_movie_recommend": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid",),
    ),
    "tmdb_movie_similar": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid",),
    ),
    "tmdb_person_credits": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbPersonPageRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("person_id", "page"),
    ),
    "tmdb_person_detail": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbPersonRequest",
        result_contract="MediaPerson | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("person_id",),
    ),
    "tmdb_seasons": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaRequest",
        result_contract="list[TmdbSeason]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid",),
    ),
    "tmdb_trending": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbTrendingRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("page",),
    ),
    "tmdb_tv_credits": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaPageRequest",
        result_contract="list[MediaPerson]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid", "page"),
    ),
    "tmdb_tv_recommend": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid",),
    ),
    "tmdb_tv_similar": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbMediaRequest",
        result_contract="list[MediaInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("tmdbid",),
    ),
    "identify_music_by_fingerprint": ModuleMethodContract(
        family="music",
        input_contract="MusicFingerprintRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("path",),
    ),
    "match_music_album": ModuleMethodContract(
        family="music",
        input_contract="MusicAlbumMatchRequest",
        result_contract="MusicAlbumInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("meta", "tracks", "limit"),
    ),
    "music_album": ModuleMethodContract(
        family="music",
        input_contract="MusicIdentityRequest",
        result_contract="MusicAlbumInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("media_source", "media_id"),
    ),
    "music_album_related": ModuleMethodContract(
        family="music",
        input_contract="MusicRelatedRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("media_source", "media_id", "count"),
    ),
    "music_artist": ModuleMethodContract(
        family="music",
        input_contract="MusicIdentityRequest",
        result_contract="MusicArtistInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("media_source", "media_id"),
    ),
    "music_artist_albums": ModuleMethodContract(
        family="music",
        input_contract="MusicArtistAlbumsRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("media_source", "media_id", "page", "count", "album_type"),
    ),
    "music_artist_related": ModuleMethodContract(
        family="music",
        input_contract="MusicRelatedRequest",
        result_contract="list[MusicArtistInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("media_source", "media_id", "count"),
    ),
    "music_cache_delete": ModuleMethodContract(
        family="music",
        input_contract="MusicCacheDeleteRequest",
        result_contract="dict | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("cache_key",),
    ),
    "music_cache_items": ModuleMethodContract(
        family="music",
        input_contract="MusicCacheReadRequest",
        result_contract="list[dict]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
    ),
    "music_cache_clear": ModuleMethodContract(
        family="music",
        input_contract="MusicCacheClearRequest",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        plugin_short_circuit=False,
    ),
    "music_chart": ModuleMethodContract(
        family="music",
        input_contract="MusicChartRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("range_name", "offset", "count", "entity"),
    ),
    "music_discover": ModuleMethodContract(
        family="music",
        input_contract="MusicDiscoverRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("media_source", "page", "count", "entity", "mode", "tags", "sort"),
    ),
    "music_fresh_releases": ModuleMethodContract(
        family="music",
        input_contract="MusicFreshReleasesRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("days", "sort", "past", "future", "offset", "count"),
    ),
    "music_lyrics": ModuleMethodContract(
        family="music",
        input_contract="MusicLyricsRequest",
        result_contract="MusicLyrics | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("music",),
    ),
    "music_lyrics_candidates": ModuleMethodContract(
        family="music",
        input_contract="MusicLyricsRequest",
        result_contract="list[MusicLyrics]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("music",),
        plugin_short_circuit=False,
    ),
    "search_music": ModuleMethodContract(
        family="music",
        input_contract="MusicSearchRequest",
        result_contract="list[MusicInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("meta", "limit", "media_source"),
    ),
    "channel_manage": ModuleMethodContract(
        family="messaging",
        input_contract="ChannelManageRequest",
        result_contract="dict[str, Any] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("channel", "action"),
    ),
    "delete_message": ModuleMethodContract(
        family="messaging",
        input_contract="MessageDeleteRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("channel", "source", "message_id", "chat_id"),
    ),
    "edit_message": ModuleMethodContract(
        family="messaging",
        input_contract="MessageEditRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("channel", "source", "message_id", "chat_id", "text", "title", "buttons", "metadata"),
    ),
    "mark_message_processing_started": ModuleMethodContract(
        family="messaging",
        input_contract="MessageProcessingStartRequest",
        result_contract="dict[str, Any] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("channel", "source", "userid", "message_id", "chat_id", "text"),
    ),
    "mark_message_processing_finished": ModuleMethodContract(
        family="messaging",
        input_contract="MessageProcessingFinishRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("channel", "source", "userid", "message_id", "chat_id", "status"),
    ),
    "message_parser": ModuleMethodContract(
        family="messaging",
        input_contract="MessageParseRequest",
        result_contract="IncomingMessage | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("source", "body", "form", "args"),
    ),
    "send_direct_message": ModuleMethodContract(
        family="messaging",
        input_contract="DirectMessageSendRequest",
        result_contract="MessageResponse | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("message",),
    ),
    "send_message": ModuleMethodContract(
        family="messaging",
        input_contract="MessageSendRequest",
        result_contract="Message | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
    ),
    "finalize_message": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFinalizeRequest",
        result_contract="Message | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("response",),
    ),
    "register_commands": ModuleMethodContract(
        family="messaging",
        input_contract="CommandRegistrationRequest",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        required_parameters=("commands",),
        plugin_short_circuit=False,
    ),
    "scheduler_job": ModuleMethodContract(
        family="scheduling",
        input_contract="SchedulerJobRequest",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        plugin_short_circuit=False,
    ),
    "webhook_parser": ModuleMethodContract(
        family="integration",
        input_contract="WebhookRequest",
        result_contract="WebhookEventInfo | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("body", "form", "args"),
    ),
    "download_discord_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_feishu_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_feishu_image_to_data_url": ModuleMethodContract(
        family="messaging",
        input_contract="MessageImageDownloadRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("image_ref", "source"),
    ),
    "download_qq_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_slack_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_slack_file_to_data_url": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_url", "source"),
    ),
    "download_synologychat_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_telegram_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_id", "source"),
    ),
    "download_telegram_file_to_base64": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_id", "source"),
    ),
    "download_vocechat_file_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageFileDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("file_ref", "source"),
    ),
    "download_vocechat_image_to_data_url": ModuleMethodContract(
        family="messaging",
        input_contract="MessageImageDownloadRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("image_ref", "source"),
    ),
    "download_wechat_image_to_data_url": ModuleMethodContract(
        family="messaging",
        input_contract="MessageImageDownloadRequest",
        result_contract="str | None",
        result_shape=ModuleResultShape.STRING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("image_ref", "source"),
    ),
    "download_wechat_media_bytes": ModuleMethodContract(
        family="messaging",
        input_contract="MessageMediaDownloadRequest",
        result_contract="bytes | None",
        result_shape=ModuleResultShape.BYTES,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("media_ref", "source"),
    ),
    "downloader_info": ModuleMethodContract(
        family="downloader",
        input_contract="DownloaderInfoRequest",
        result_contract="list[DownloaderInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("downloader",),
    ),
    "list_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentListRequest",
        result_contract="list[DownloaderTorrent]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("status", "hashs", "downloader", "include_all_tags"),
    ),
    "filter_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentFilterRequest",
        result_contract="list[TorrentInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("rule_groups", "torrent_list", "mediainfo"),
    ),
    "refresh_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentRefreshRequest",
        result_contract="list[TorrentInfo]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("site", "keyword", "cat", "page", "mtype"),
    ),
    "torrent_files": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentFilesRequest",
        result_contract="list[DownloaderFile]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("tid", "downloader"),
    ),
    "get_torrent_trackers": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentTrackersRequest",
        result_contract="dict[str, list[str]] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.ORDERED_MAPPING_MERGE,
        required_parameters=("hash_string", "downloader"),
    ),
    "download": ModuleMethodContract(
        family="downloader",
        input_contract="DownloadTaskRequest",
        result_contract="DownloadTaskResult | None",
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("content", "download_dir", "cookie", "episodes", "category", "label", "downloader"),
    ),
    "download_added": ModuleMethodContract(
        family="downloader",
        input_contract="DownloadAddedHook",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        required_parameters=("context", "torrent_content", "download_dir"),
        plugin_short_circuit=False,
    ),
    "remove_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentRemoveRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("hashs", "delete_file", "downloader"),
    ),
    "set_torrents_tag": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentTagRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("hashs", "tags", "downloader"),
    ),
    "start_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentControlRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("hashs", "downloader"),
    ),
    "stop_torrents": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentControlRequest",
        result_contract="bool | None",
        result_shape=ModuleResultShape.BOOLEAN,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("hashs", "downloader"),
    ),
    "update_torrent": ModuleMethodContract(
        family="downloader",
        input_contract="TorrentUpdateRequest",
        result_contract="dict[str, bool] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=(
            "hash_string",
            "downloader",
            "download_limit",
            "upload_limit",
            "tracker_list",
            "save_path",
            "category",
            "ratio_limit",
            "seeding_time_limit",
        ),
    ),
    "transfer_completed": ModuleMethodContract(
        family="downloader",
        input_contract="TransferCompletedHook",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        required_parameters=("hashs", "downloader"),
        plugin_short_circuit=False,
    ),
    "tmdb_cache_items": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbCacheListRequest",
        result_contract="list[dict[str, Any]]",
        result_shape=ModuleResultShape.LIST,
        aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
    ),
    "tmdb_cache_delete": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbCacheDeleteRequest",
        result_contract="dict[str, Any] | None",
        result_shape=ModuleResultShape.MAPPING,
        aggregation=ModuleResultAggregation.FIRST_NON_EMPTY,
        required_parameters=("cache_key",),
    ),
    "tmdb_cache_clear": ModuleMethodContract(
        family="tmdb",
        input_contract="TmdbCacheClearRequest",
        result_contract="None",
        aggregation=ModuleResultAggregation.FAN_OUT,
        plugin_short_circuit=False,
    ),
}

# 同一能力的同步/异步入口共享不可变契约对象，避免参数和聚合语义各自漂移。
_METHOD_CONTRACTS.update(
    {
        "async_recognize_media": _METHOD_CONTRACTS["recognize_media"],
        "async_match_doubaninfo": _METHOD_CONTRACTS["match_doubaninfo"],
        "async_match_tmdbinfo": _METHOD_CONTRACTS["match_tmdbinfo"],
        "async_update_recognize_cache": _METHOD_CONTRACTS["update_recognize_cache"],
        "async_search_medias": _METHOD_CONTRACTS["search_medias"],
        "async_get_media_auxiliary_info": _METHOD_CONTRACTS["get_media_auxiliary_info"],
        "async_obtain_images": _METHOD_CONTRACTS["obtain_images"],
        "async_movie_hot": _METHOD_CONTRACTS["movie_hot"],
        "async_movie_showing": _METHOD_CONTRACTS["movie_showing"],
        "async_movie_top250": _METHOD_CONTRACTS["movie_top250"],
        "async_search_collections": _METHOD_CONTRACTS["search_collections"],
        "async_search_persons": _METHOD_CONTRACTS["search_persons"],
        "async_search_subtitles": _METHOD_CONTRACTS["search_subtitles"],
        "async_search_torrents": _METHOD_CONTRACTS["search_torrents"],
        "async_tv_animation": _METHOD_CONTRACTS["tv_animation"],
        "async_tv_hot": _METHOD_CONTRACTS["tv_hot"],
        "async_tv_weekly_chinese": _METHOD_CONTRACTS["tv_weekly_chinese"],
        "async_tv_weekly_global": _METHOD_CONTRACTS["tv_weekly_global"],
        "async_douban_discover": _METHOD_CONTRACTS["douban_discover"],
        "async_douban_info": _METHOD_CONTRACTS["douban_info"],
        "async_douban_movie_credits": _METHOD_CONTRACTS["douban_movie_credits"],
        "async_douban_movie_recommend": _METHOD_CONTRACTS["douban_movie_recommend"],
        "async_douban_person_credits": _METHOD_CONTRACTS["douban_person_credits"],
        "async_douban_person_detail": _METHOD_CONTRACTS["douban_person_detail"],
        "async_douban_tv_credits": _METHOD_CONTRACTS["douban_tv_credits"],
        "async_douban_tv_recommend": _METHOD_CONTRACTS["douban_tv_recommend"],
        "async_bangumi_calendar": _METHOD_CONTRACTS["bangumi_calendar"],
        "async_bangumi_credits": _METHOD_CONTRACTS["bangumi_credits"],
        "async_bangumi_discover": _METHOD_CONTRACTS["bangumi_discover"],
        "async_bangumi_info": _METHOD_CONTRACTS["bangumi_info"],
        "async_bangumi_person_credits": _METHOD_CONTRACTS["bangumi_person_credits"],
        "async_bangumi_person_detail": _METHOD_CONTRACTS["bangumi_person_detail"],
        "async_bangumi_recommend": _METHOD_CONTRACTS["bangumi_recommend"],
        "async_anilist_credits": _METHOD_CONTRACTS["anilist_credits"],
        "async_anilist_discover": _METHOD_CONTRACTS["anilist_discover"],
        "async_anilist_info": _METHOD_CONTRACTS["anilist_info"],
        "async_anilist_person_credits": _METHOD_CONTRACTS["anilist_person_credits"],
        "async_anilist_person_detail": _METHOD_CONTRACTS["anilist_person_detail"],
        "async_anilist_popular_this_season": _METHOD_CONTRACTS["anilist_popular_this_season"],
        "async_anilist_recommendations": _METHOD_CONTRACTS["anilist_recommendations"],
        "async_anilist_trending": _METHOD_CONTRACTS["anilist_trending"],
        "async_tmdb_collection": _METHOD_CONTRACTS["tmdb_collection"],
        "async_tmdb_discover": _METHOD_CONTRACTS["tmdb_discover"],
        "async_tmdb_episodes": _METHOD_CONTRACTS["tmdb_episodes"],
        "async_tmdb_group_seasons": _METHOD_CONTRACTS["tmdb_group_seasons"],
        "async_tmdb_info": _METHOD_CONTRACTS["tmdb_info"],
        "async_tmdb_movie_credits": _METHOD_CONTRACTS["tmdb_movie_credits"],
        "async_tmdb_movie_recommend": _METHOD_CONTRACTS["tmdb_movie_recommend"],
        "async_tmdb_movie_similar": _METHOD_CONTRACTS["tmdb_movie_similar"],
        "async_tmdb_person_credits": _METHOD_CONTRACTS["tmdb_person_credits"],
        "async_tmdb_person_detail": _METHOD_CONTRACTS["tmdb_person_detail"],
        "async_tmdb_seasons": _METHOD_CONTRACTS["tmdb_seasons"],
        "async_tmdb_trending": _METHOD_CONTRACTS["tmdb_trending"],
        "async_tmdb_tv_credits": _METHOD_CONTRACTS["tmdb_tv_credits"],
        "async_tmdb_tv_recommend": _METHOD_CONTRACTS["tmdb_tv_recommend"],
        "async_tmdb_tv_similar": _METHOD_CONTRACTS["tmdb_tv_similar"],
        "async_identify_music_by_fingerprint": _METHOD_CONTRACTS["identify_music_by_fingerprint"],
        "async_match_music_album": _METHOD_CONTRACTS["match_music_album"],
        "async_refresh_torrents": _METHOD_CONTRACTS["refresh_torrents"],
    }
)

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
    "anilist_credits",
    "anilist_discover",
    "anilist_info",
    "anilist_person_credits",
    "anilist_person_detail",
    "anilist_popular_this_season",
    "anilist_recommendations",
    "anilist_trending",
    "any_files",
    "async_anilist_credits",
    "async_anilist_discover",
    "async_anilist_info",
    "async_anilist_person_credits",
    "async_anilist_person_detail",
    "async_anilist_popular_this_season",
    "async_anilist_recommendations",
    "async_anilist_trending",
    "async_bangumi_calendar",
    "async_bangumi_credits",
    "async_bangumi_discover",
    "async_bangumi_info",
    "async_bangumi_person_credits",
    "async_bangumi_person_detail",
    "async_bangumi_recommend",
    "async_douban_discover",
    "async_douban_info",
    "async_douban_movie_credits",
    "async_douban_movie_recommend",
    "async_douban_person_credits",
    "async_douban_person_detail",
    "async_douban_tv_credits",
    "async_douban_tv_recommend",
    "async_identify_music_by_fingerprint",
    "async_match_doubaninfo",
    "async_match_music_album",
    "async_match_tmdbinfo",
    "async_movie_hot",
    "async_movie_showing",
    "async_movie_top250",
    "async_obtain_images",
    "async_recognize_media",
    "async_refresh_torrents",
    "async_search_collections",
    "async_search_medias",
    "async_search_persons",
    "async_search_subtitles",
    "async_search_torrents",
    "async_tmdb_collection",
    "async_tmdb_discover",
    "async_tmdb_episodes",
    "async_tmdb_group_seasons",
    "async_tmdb_info",
    "async_tmdb_movie_credits",
    "async_tmdb_movie_recommend",
    "async_tmdb_movie_similar",
    "async_tmdb_person_credits",
    "async_tmdb_person_detail",
    "async_tmdb_seasons",
    "async_tmdb_trending",
    "async_tmdb_tv_credits",
    "async_tmdb_tv_recommend",
    "async_tmdb_tv_similar",
    "async_tv_animation",
    "async_tv_hot",
    "async_tv_weekly_chinese",
    "async_tv_weekly_global",
    "async_update_recognize_cache",
    "bangumi_calendar",
    "bangumi_credits",
    "bangumi_discover",
    "bangumi_info",
    "bangumi_person_credits",
    "bangumi_person_detail",
    "bangumi_recommend",
    "channel_manage",
    "clear_cache",
    "create_folder",
    "delete_file",
    "delete_message",
    "douban_discover",
    "douban_info",
    "douban_movie_credits",
    "douban_movie_recommend",
    "douban_person_credits",
    "douban_person_detail",
    "douban_tv_credits",
    "douban_tv_recommend",
    "download",
    "download_added",
    "download_discord_file_bytes",
    "download_feishu_file_bytes",
    "download_feishu_image_to_data_url",
    "download_file",
    "download_qq_file_bytes",
    "download_slack_file_bytes",
    "download_slack_file_to_data_url",
    "download_synologychat_file_bytes",
    "download_telegram_file_bytes",
    "download_telegram_file_to_base64",
    "download_vocechat_file_bytes",
    "download_vocechat_image_to_data_url",
    "download_wechat_image_to_data_url",
    "download_wechat_media_bytes",
    "downloader_info",
    "edit_message",
    "filter_torrents",
    "finalize_message",
    "get_file_item",
    "get_folder",
    "get_parent_item",
    "get_search_page_size",
    "get_torrent_trackers",
    "identify_music_by_fingerprint",
    "list_files",
    "list_torrents",
    "mark_message_processing_finished",
    "mark_message_processing_started",
    "match_doubaninfo",
    "match_music_album",
    "match_tmdbinfo",
    "media_exists",
    "media_files",
    "media_statistic",
    "mediaserver_image_cookies",
    "mediaserver_iteminfo",
    "mediaserver_items",
    "mediaserver_items_count",
    "mediaserver_latest",
    "mediaserver_latest_images",
    "mediaserver_librarys",
    "mediaserver_play_url",
    "mediaserver_playing",
    "mediaserver_season_episode_ids",
    "mediaserver_tv_episodes",
    "message_parser",
    "metadata_img",
    "metadata_nfo",
    "movie_hot",
    "movie_showing",
    "movie_top250",
    "music_album",
    "music_album_related",
    "music_artist",
    "music_artist_albums",
    "music_artist_related",
    "music_cache_clear",
    "music_cache_delete",
    "music_cache_items",
    "music_chart",
    "music_discover",
    "music_fresh_releases",
    "music_lyrics",
    "obtain_images",
    "obtain_specific_image",
    "recognize_media",
    "recommend_name",
    "refresh_torrents",
    "refresh_userdata",
    "register_commands",
    "remove_torrents",
    "rename_file",
    "scheduler_job",
    "search_collections",
    "search_medias",
    "search_music",
    "search_persons",
    "search_subtitles",
    "search_torrents",
    "search_tvdb",
    "send_direct_message",
    "set_torrents_tag",
    "site_subtitle_links",
    "snapshot_storage",
    "start_torrents",
    "stop_torrents",
    "storage_manage",
    "tmdb_cache_clear",
    "tmdb_cache_delete",
    "tmdb_cache_items",
    "tmdb_collection",
    "tmdb_discover",
    "tmdb_episodes",
    "tmdb_group_seasons",
    "tmdb_info",
    "tmdb_movie_credits",
    "tmdb_movie_recommend",
    "tmdb_movie_similar",
    "tmdb_person_credits",
    "tmdb_person_detail",
    "tmdb_seasons",
    "tmdb_trending",
    "tmdb_tv_credits",
    "tmdb_tv_recommend",
    "tmdb_tv_similar",
    "torrent_files",
    "transfer",
    "transfer_completed",
    "tv_animation",
    "tv_hot",
    "tv_weekly_chinese",
    "tv_weekly_global",
    "tvdb_info",
    "tvdb_slug",
    "update_recognize_cache",
    "update_torrent",
    "upload_file",
    "user_authenticate",
    "webhook_parser",
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
    if method.startswith(
        (
            "download",
            "torrent_",
            "list_torrents",
            "refresh_torrents",
            "remove_torrents",
            "start_torrents",
            "stop_torrents",
            "set_torrents_tag",
            "update_torrent",
            "get_torrent_trackers",
            "downloader_info",
            "filter_torrents",
            "transfer_completed",
        )
    ):
        return "downloader"
    if method.startswith(
        (
            "channel_",
            "delete_message",
            "edit_message",
            "finalize_message",
            "mark_message_",
            "message_parser",
            "register_commands",
            "send_direct_message",
            "send_message",
        )
    ):
        return "messaging"
    if method.startswith(
        (
            "any_files",
            "create_folder",
            "delete_file",
            "get_file_item",
            "get_folder",
            "get_parent_item",
            "list_files",
            "media_files",
            "rename_file",
            "snapshot_storage",
            "storage_manage",
            "transfer",
            "upload_file",
        )
    ):
        return "storage"
    if method.startswith(
        (
            "metadata_",
            "obtain_specific_image",
            "recommend_name",
        )
    ):
        return "metadata"
    if method.startswith(
        (
            "async_identify_music",
            "async_match_music",
            "identify_music",
            "match_music",
            "search_music",
        )
    ):
        return "music"
    if method.startswith(
        (
            "async_get_media_auxiliary_info",
            "async_match_",
            "async_obtain_images",
            "async_recognize_media",
            "async_update_recognize_cache",
            "match_",
            "obtain_images",
            "get_media_auxiliary_info",
            "recognize_media",
            "update_recognize_cache",
        )
    ):
        return "media-recognition"
    if method.startswith(
        (
            "async_movie_",
            "async_search_",
            "async_tv_",
            "movie_",
            "search_collections",
            "search_medias",
            "search_persons",
            "search_subtitles",
            "search_torrents",
            "tv_",
        )
    ):
        return "media-discovery"
    if method == "clear_cache":
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
    except TypeError, ValueError:
        return ("signature-unavailable",)
    missing = tuple(
        name
        for name in contract.required_parameters
        if name not in parameters
        and not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
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
