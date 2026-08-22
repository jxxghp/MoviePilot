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
        result_contract="list[MediaInfo]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE,
        required_parameters=("meta", "media_source"),
    ),
    "obtain_images": ModuleMethodContract(family="media-recognition", input_contract="MediaInfo", result_contract="MediaInfo | None", required_parameters=("mediainfo",)),
    "media_category": ModuleMethodContract(family="media-recognition", input_contract="MediaCategoryRequest", result_contract="CategoryConfig | None"),
    "mediaserver_items": ModuleMethodContract(family="media-server", input_contract="MediaServerItemsRequest", result_contract="list[MediaServerItem]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("server", "library_id", "start_index", "limit")),
    "mediaserver_iteminfo": ModuleMethodContract(family="media-server", input_contract="MediaServerItemRequest", result_contract="MediaServerItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("server", "item_id")),
    "mediaserver_play_url": ModuleMethodContract(family="media-server", input_contract="MediaServerPlayRequest", result_contract="str | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("server", "item_id")),
    "mediaserver_tv_episodes": ModuleMethodContract(family="media-server", input_contract="MediaServerEpisodesRequest", result_contract="list[MediaServerPlayItem]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("server", "item_id")),
    "download_file": ModuleMethodContract(family="storage", input_contract="StorageDownloadRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "path")),
    "upload_file": ModuleMethodContract(family="storage", input_contract="StorageUploadRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "path", "new_name")),
    "list_files": ModuleMethodContract(family="storage", input_contract="StorageListRequest", result_contract="list[FileItem]", aggregation=ModuleResultAggregation.ORDERED_LIST_MERGE, required_parameters=("fileitem", "recursion")),
    "get_file_item": ModuleMethodContract(family="storage", input_contract="StorageItemRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path")),
    "get_folder": ModuleMethodContract(family="storage", input_contract="StorageFolderRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path")),
    "get_parent_item": ModuleMethodContract(family="storage", input_contract="StorageParentRequest", result_contract="FileItem | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem",)),
    "rename_file": ModuleMethodContract(family="storage", input_contract="StorageRenameRequest", result_contract="bool | FileItem", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("fileitem", "name")),
    "storage_manage": ModuleMethodContract(family="storage", input_contract="StorageManageRequest", result_contract="Any", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "action")),
    "snapshot_storage": ModuleMethodContract(family="storage", input_contract="StorageSnapshotRequest", result_contract="dict[str, dict] | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("storage", "path", "last_snapshot_time", "max_depth", "previous_snapshot")),
    "send_message": ModuleMethodContract(family="messaging", input_contract="MessageSendRequest", result_contract="Message | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY),
    "finalize_message": ModuleMethodContract(family="messaging", input_contract="MessageFinalizeRequest", result_contract="Message | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("response",)),
    "register_commands": ModuleMethodContract(family="messaging", input_contract="CommandRegistrationRequest", result_contract="None", required_parameters=("commands",)),
    "scheduler_job": ModuleMethodContract(family="scheduling", input_contract="SchedulerJobRequest", result_contract="None"),
    "webhook_parser": ModuleMethodContract(family="integration", input_contract="WebhookRequest", result_contract="WebhookEventInfo | None", aggregation=ModuleResultAggregation.FIRST_NON_EMPTY, required_parameters=("body", "form", "args")),
}

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


def list_explicit_module_contracts() -> dict[str, ModuleMethodContract]:
    """返回显式方法清单的副本，供架构基线和 SDK 文档使用。"""
    return dict(_METHOD_CONTRACTS)
