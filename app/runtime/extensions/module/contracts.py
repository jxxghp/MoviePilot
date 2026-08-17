"""字符串模块方法协议的可检查契约清单。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModuleResultAggregation(StrEnum):
    """描述多模块结果沿调用链的兼容聚合方式。"""

    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class ModuleMethodContract:
    """记录一个模块方法族的调用模式和结果规则。"""

    family: str
    aggregation: ModuleResultAggregation = ModuleResultAggregation.LEGACY
    supports_sync: bool = True
    supports_async: bool = True
    plugin_short_circuit: bool = True


_DEFAULT_CONTRACT = ModuleMethodContract(family="legacy")

# 首批登记高频能力族。方法名仍保持开放字符串，以兼容第三方插件自定义模块能力；
# 未命中项继续使用冻结的 legacy 规则，并由架构快照记录新增调用位置。
_METHOD_CONTRACTS = {
    "recognize_media": ModuleMethodContract(family="media-recognition"),
    "search_medias": ModuleMethodContract(family="media-recognition"),
    "obtain_images": ModuleMethodContract(family="media-recognition"),
    "media_category": ModuleMethodContract(family="media-recognition"),
    "mediaserver_items": ModuleMethodContract(family="media-server"),
    "mediaserver_iteminfo": ModuleMethodContract(family="media-server"),
    "mediaserver_play_url": ModuleMethodContract(family="media-server"),
    "mediaserver_tv_episodes": ModuleMethodContract(family="media-server"),
    "download_file": ModuleMethodContract(family="storage"),
    "upload_file": ModuleMethodContract(family="storage"),
    "list_files": ModuleMethodContract(family="storage"),
    "get_file_item": ModuleMethodContract(family="storage"),
    "get_folder": ModuleMethodContract(family="storage"),
    "get_parent_item": ModuleMethodContract(family="storage"),
    "rename_file": ModuleMethodContract(family="storage"),
    "storage_manage": ModuleMethodContract(family="storage"),
    "snapshot_storage": ModuleMethodContract(family="storage"),
    "send_message": ModuleMethodContract(family="messaging"),
    "finalize_message": ModuleMethodContract(family="messaging"),
    "register_commands": ModuleMethodContract(family="messaging"),
    "scheduler_job": ModuleMethodContract(family="scheduling"),
    "webhook_parser": ModuleMethodContract(family="integration"),
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
