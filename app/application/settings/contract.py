"""Agent 系统设置的静态合同、Schema 和专用操作边界。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, cast

from pydantic import TypeAdapter
from pydantic_core import PydanticUndefined
from typing_extensions import TypedDict

from app.application.security.secrets import is_secret_setting_key
from app.runtime.config import Settings
from app.schemas.agent import AgentMcpServerConfig
from app.schemas.category import ClassificationPolicyState
from app.schemas.common import JsonData
from app.schemas.plugin import PluginFoldersData
from app.schemas.rule import CustomRule, FilterRuleGroup
from app.schemas.site import SiteAuth
from app.schemas.subscribe import Subscribe
from app.schemas.system import (
    DownloaderConf,
    MediaServerConf,
    NotificationConf,
    NotificationSwitchConf,
    StorageConf,
    TransferDirectoryConf,
)
from app.schemas.transfer import EpisodeFormatRule
from app.schemas.types import SystemConfigKey

SettingOperation = Literal[
    "replace",
    "merge_dict",
    "upsert_list_item",
    "remove_list_item",
]
SettingApplyMode = Literal[
    "immediate",
    "restart_required",
    "dedicated_operation",
    "internal_state",
]


class NotificationDeliveryPeriod(TypedDict):
    """通知发送允许时间段。"""

    start: str
    end: str


class NotificationClearState(TypedDict, total=False):
    """各通知范围最后一次清理前的毫秒时间戳。"""

    all: int
    system: int
    media: int


class SubscribeDefaultParams(TypedDict, total=False):
    """订阅搜索流程使用的全局默认过滤参数。"""

    include: str
    exclude: str
    quality: str
    resolution: str
    effect: str
    audio_quality: str
    audio_format: str
    min_bitrate: int
    min_bit_depth: int
    min_sample_rate: int
    tv_size: str
    movie_size: str
    min_seeders: int
    min_seeders_time: int


ScrapingPolicyValue = bool | Literal["skip", "missingOnly", "always", "upgrade"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """描述一个可被 Agent 发现、读取或受控更新的设置项。"""

    key: str
    source: Literal["settings", "systemconfig"]
    group: str
    label: str
    description: str = ""
    value_annotation: Any = field(default=Any, repr=False, compare=False)
    declared_type: str = "unknown"
    update_operations: tuple[SettingOperation, ...] = ()
    generic_write_allowed: bool = True
    preferred_operation_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    apply_mode: SettingApplyMode = "immediate"
    default_match_field: Optional[str] = None
    unit: Optional[str] = None
    examples: tuple[Any, ...] = field(default_factory=tuple, repr=False, compare=False)
    sensitive: bool = False
    systemconfig_key: Optional[SystemConfigKey] = None
    source_file: str = ""
    source_line: int = 0


@dataclass(frozen=True, slots=True)
class _SystemConfigContract:
    """定义一个 SystemConfigKey 的值结构和修改边界。"""

    group: str
    value_annotation: Any = field(repr=False, compare=False)
    update_operations: tuple[SettingOperation, ...] = ("replace",)
    generic_write_allowed: bool = True
    preferred_operation_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    apply_mode: SettingApplyMode = "immediate"
    default_match_field: Optional[str] = None
    unit: Optional[str] = None
    examples: tuple[Any, ...] = field(default_factory=tuple, repr=False, compare=False)
    sensitive: bool = False


def _contract(
    group: str,
    value_annotation: Any,
    *,
    operations: tuple[SettingOperation, ...] = ("replace",),
    writable: bool = True,
    preferred: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    apply_mode: SettingApplyMode = "immediate",
    match_field: Optional[str] = None,
    unit: Optional[str] = None,
    examples: tuple[Any, ...] = (),
    sensitive: bool = False,
) -> _SystemConfigContract:
    """以紧凑参数构造一个数据库设置合同。"""
    return _SystemConfigContract(
        group=group,
        value_annotation=value_annotation,
        update_operations=operations if writable else (),
        generic_write_allowed=writable,
        preferred_operation_ids=preferred,
        dependencies=dependencies,
        conflicts=conflicts,
        apply_mode=apply_mode,
        default_match_field=match_field,
        unit=unit,
        examples=examples,
        sensitive=sensitive,
    )


def _managed_contract(
    group: str,
    value_annotation: Any,
    *preferred: str,
    sensitive: bool = False,
) -> _SystemConfigContract:
    """构造必须经专用 operation 修改的数据库设置合同。"""
    return _contract(
        group,
        value_annotation,
        writable=False,
        preferred=tuple(preferred),
        apply_mode="dedicated_operation",
        sensitive=sensitive,
    )


def _internal_contract(
    group: str,
    value_annotation: Any,
    *,
    sensitive: bool = False,
) -> _SystemConfigContract:
    """构造只允许业务 owner 维护的内部状态合同。"""
    return _contract(
        group,
        value_annotation,
        writable=False,
        apply_mode="internal_state",
        sensitive=sensitive,
    )


_LIST_OBJECT_OPERATIONS: tuple[SettingOperation, ...] = (
    "replace",
    "upsert_list_item",
    "remove_list_item",
)
_LIST_SCALAR_OPERATIONS: tuple[SettingOperation, ...] = (
    "replace",
    "upsert_list_item",
    "remove_list_item",
)
_OBJECT_OPERATIONS: tuple[SettingOperation, ...] = ("replace", "merge_dict")

SYSTEMCONFIG_CONTRACTS: dict[SystemConfigKey, _SystemConfigContract] = {
    SystemConfigKey.Downloaders: _contract(
        "downloaders",
        Optional[list[DownloaderConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="name",
    ),
    SystemConfigKey.MediaServers: _contract(
        "media_servers",
        Optional[list[MediaServerConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="name",
    ),
    SystemConfigKey.Notifications: _contract(
        "notifications",
        Optional[list[NotificationConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="name",
        sensitive=True,
    ),
    SystemConfigKey.NotificationSwitchs: _contract(
        "notification_switches",
        Optional[list[NotificationSwitchConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="type",
    ),
    SystemConfigKey.Directories: _contract(
        "directories",
        Optional[list[TransferDirectoryConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="name",
    ),
    SystemConfigKey.MountedLocalDiskDeleteEmptyDirs: _contract(
        "directories", Optional[bool]
    ),
    SystemConfigKey.Storages: _contract(
        "storages",
        Optional[list[StorageConf]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="name",
        sensitive=True,
    ),
    SystemConfigKey.IndexerSites: _contract(
        "search_sites",
        Optional[list[int]],
        operations=_LIST_SCALAR_OPERATIONS,
    ),
    SystemConfigKey.RssSites: _contract(
        "subscribe_sites",
        Optional[list[int]],
        operations=_LIST_SCALAR_OPERATIONS,
    ),
    SystemConfigKey.CustomReleaseGroups: _contract(
        "recognition_words",
        Optional[list[str] | str],
        operations=("replace",),
        examples=([], ["Group A", "字幕组"]),
    ),
    SystemConfigKey.Customization: _contract(
        "recognition_words",
        Optional[list[str] | str],
        operations=("replace",),
        examples=([], ["{title}", "{year}"]),
    ),
    SystemConfigKey.CustomIdentifiers: _managed_contract(
        "recognition_words",
        Optional[list[str]],
        "config.identifiers.update",
    ),
    SystemConfigKey.EpisodeFormatRuleTable: _contract(
        "transfer",
        Optional[list[EpisodeFormatRule]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="id",
    ),
    SystemConfigKey.TransferExcludeWords: _contract(
        "transfer",
        Optional[list[str]],
        operations=_LIST_SCALAR_OPERATIONS,
    ),
    SystemConfigKey.TorrentsPriority: _contract(
        "filter_rules",
        Optional[list[str]],
        operations=_LIST_SCALAR_OPERATIONS,
    ),
    SystemConfigKey.CustomFilterRules: _managed_contract(
        "filter_rules",
        Optional[list[CustomRule]],
        "filter.custom.add",
        "filter.custom.update",
        "filter.custom.delete",
    ),
    SystemConfigKey.UserFilterRuleGroups: _managed_contract(
        "filter_rules",
        Optional[list[FilterRuleGroup]],
        "filter.group.add",
        "filter.group.update",
        "filter.group.delete",
    ),
    SystemConfigKey.SearchFilterRuleGroups: _contract(
        "filter_defaults",
        Optional[list[str]],
        operations=_LIST_SCALAR_OPERATIONS,
        dependencies=(SystemConfigKey.UserFilterRuleGroups.value,),
    ),
    SystemConfigKey.SubscribeFilterRuleGroups: _contract(
        "filter_defaults",
        Optional[list[str]],
        operations=_LIST_SCALAR_OPERATIONS,
        dependencies=(SystemConfigKey.UserFilterRuleGroups.value,),
    ),
    SystemConfigKey.SubscribeDefaultParams: _contract(
        "subscribe_defaults",
        Optional[SubscribeDefaultParams],
        operations=_OBJECT_OPERATIONS,
        examples=({"quality": "WEB-DL", "resolution": "1080p"},),
    ),
    SystemConfigKey.BestVersionFilterRuleGroups: _contract(
        "filter_defaults",
        Optional[list[str]],
        operations=_LIST_SCALAR_OPERATIONS,
        dependencies=(SystemConfigKey.UserFilterRuleGroups.value,),
    ),
    SystemConfigKey.SubscribeReport: _internal_contract(
        "telemetry", Optional[dict[str, JsonData]]
    ),
    SystemConfigKey.UserCustomCSS: _contract("personalization", Optional[str]),
    SystemConfigKey.UserInstalledPlugins: _managed_contract(
        "plugins",
        Optional[list[str]],
        "plugin.install",
        "plugin.uninstall",
    ),
    SystemConfigKey.PluginInstances: _managed_contract(
        "plugins",
        Optional[dict[str, JsonData]],
        "plugin.clone",
        "plugin.instance.set_enabled",
        "plugin.instance.purge",
        sensitive=True,
    ),
    SystemConfigKey.PluginInstancesImported: _internal_contract(
        "plugins", Optional[dict[str, str]]
    ),
    SystemConfigKey.PluginFolders: _managed_contract(
        "plugins",
        Optional[PluginFoldersData],
        "plugin.folders.update",
        "plugin.folder.create",
        "plugin.folder.update",
        "plugin.folder.delete",
        "plugin.folder.plugins.update",
        "plugin.folder.plugin.assign",
        "plugin.folder.plugin.remove",
    ),
    SystemConfigKey.DefaultMovieSubscribeConfig: _contract(
        "subscribe_defaults", Optional[Subscribe], operations=_OBJECT_OPERATIONS
    ),
    SystemConfigKey.DefaultTvSubscribeConfig: _contract(
        "subscribe_defaults", Optional[Subscribe], operations=_OBJECT_OPERATIONS
    ),
    SystemConfigKey.DefaultMusicSubscribeConfig: _contract(
        "subscribe_defaults", Optional[Subscribe], operations=_OBJECT_OPERATIONS
    ),
    SystemConfigKey.UserSiteAuthParams: _managed_contract(
        "site_auth",
        Optional[list[SiteAuth]],
        "site.auth.options",
        "site.authenticate",
        sensitive=True,
    ),
    SystemConfigKey.FollowSubscribers: _managed_contract(
        "subscribe_sharing",
        Optional[list[str]],
        "subscription.follow.list",
        "subscription.follow.add",
        "subscription.follow.delete",
    ),
    SystemConfigKey.NotificationSendTime: _contract(
        "notifications",
        Optional[NotificationDeliveryPeriod | list[NotificationDeliveryPeriod]],
        operations=("replace",),
        examples=({"start": "08:00", "end": "23:00"},),
    ),
    SystemConfigKey.AIAgentConfig: _internal_contract(
        "ai_agent", Optional[dict[str, JsonData]], sensitive=True
    ),
    SystemConfigKey.AIAgentMcpServers: _contract(
        "ai_agent",
        Optional[list[AgentMcpServerConfig]],
        operations=_LIST_OBJECT_OPERATIONS,
        match_field="id",
        sensitive=True,
    ),
    SystemConfigKey.NotificationTemplates: _contract(
        "notifications", Optional[dict[str, str]], operations=_OBJECT_OPERATIONS
    ),
    SystemConfigKey.NotificationClearBefore: _internal_contract(
        "notifications", Optional[NotificationClearState]
    ),
    SystemConfigKey.ScrapingSwitchs: _contract(
        "scraping",
        Optional[dict[str, ScrapingPolicyValue]],
        operations=_OBJECT_OPERATIONS,
    ),
    SystemConfigKey.PluginInstallReport: _internal_contract(
        "telemetry", Optional[dict[str, JsonData]]
    ),
    SystemConfigKey.UgreenSessionCache: _internal_contract(
        "media_servers", Optional[dict[str, JsonData]], sensitive=True
    ),
    SystemConfigKey.MediaRecognizeShareCount: _internal_contract(
        "telemetry", Optional[int]
    ),
    SystemConfigKey.MediaClassificationPolicy: _managed_contract(
        "media_classification",
        Optional[ClassificationPolicyState],
        "media.classification.policy.get",
        "media.classification.policy.validate",
        "media.classification.policy.preview",
        "media.classification.policy.impact",
        "media.classification.policy.history",
        "media.classification.policy.update",
        "media.classification.policy.rollback",
    ),
}

_RESTART_REQUIRED_GROUPS = {
    "application",
    "authentication",
    "cache",
    "cloud_storage",
    "database",
    "database_backup",
    "dependencies",
    "logging",
    "network",
    "performance",
    "security",
    "system_update",
}


def _load_source_catalog() -> dict[str, Any]:
    """读取由源码注释生成的设置来源目录。"""
    catalog_path = Path(__file__).with_name("resources") / "system_settings_catalog.json"
    return cast(dict[str, Any], json.loads(catalog_path.read_text(encoding="utf-8")))


def _format_declared_type(annotation: Any) -> str:
    """把字段注解转换为稳定且便于 Agent 阅读的类型文本。"""
    rendered = str(annotation).replace("typing.", "")
    return rendered.replace("<class '", "").replace("'>", "")


def _runtime_dependencies(key: str) -> tuple[str, ...]:
    """返回可由键名前缀可靠推导的运行时依赖项。"""
    if key.startswith("AUDIO_INPUT_"):
        return ("AI_AGENT_ENABLE", "LLM_SUPPORT_AUDIO_INPUT")
    if key.startswith("AUDIO_OUTPUT_"):
        return ("AI_AGENT_ENABLE", "LLM_SUPPORT_AUDIO_OUTPUT")
    if key.startswith("LLM_") or key.startswith("AI_AGENT_"):
        return () if key == "AI_AGENT_ENABLE" else ("AI_AGENT_ENABLE",)
    if key.startswith("DB_POSTGRESQL_") or key.startswith("DB_SQLITE_"):
        return ("DB_TYPE",)
    if key.startswith("CACHE_REDIS_"):
        return ("CACHE_BACKEND_TYPE",)
    if key in {"DOH_DOMAINS", "DOH_RESOLVERS"}:
        return ("DOH_ENABLE",)
    if key in {"BANGUMI_API_DOMAIN", "BANGUMI_IMAGE_DOMAIN"}:
        return ("BANGUMI_PROXY_ENABLE",)
    if key in {"WALLPAPER_IMAGE_URL", "CUSTOMIZE_WALLPAPER_API_URL"}:
        return ("WALLPAPER",)
    return ()


def _runtime_unit(key: str) -> Optional[str]:
    """按稳定字段后缀推导运行时数值的单位提示。"""
    suffix_units = (
        ("_MINUTES", "minutes"),
        ("_SECONDS", "seconds"),
        ("_HOURS", "hours"),
        ("_DAYS", "days"),
        ("_BYTES", "bytes"),
        ("_PORT", "port"),
        ("_COUNT", "count"),
        ("_SIZE", "count"),
        ("_THREADS", "threads"),
        ("_TOKENS", "tokens"),
        ("_TIMEOUT", "seconds"),
    )
    for suffix, unit in suffix_units:
        if key.endswith(suffix):
            return unit
    return None


def _runtime_examples(model_field: Any, *, sensitive: bool) -> tuple[Any, ...]:
    """从无敏感默认值生成一个可序列化的示例集合。"""
    if sensitive or model_field.default is PydanticUndefined or model_field.default is None:
        return ()
    try:
        json.dumps(model_field.default, ensure_ascii=False)
    except (TypeError, ValueError):
        return ()
    return (model_field.default,)


def build_setting_specs() -> tuple[dict[str, SettingSpec], dict[str, SettingSpec]]:
    """从 Pydantic 字段、来源目录和数据库合同构建完整设置索引。"""
    catalog = _load_source_catalog()
    runtime_catalog = catalog.get("settings", {})
    system_catalog = catalog.get("systemconfig", {})
    core_specs: dict[str, SettingSpec] = {}
    for key, model_field in Settings.model_fields.items():
        metadata = runtime_catalog.get(key)
        if not isinstance(metadata, dict):
            raise ValueError(f"Settings 来源目录缺少配置项: {key}")
        description = str(metadata.get("description") or "").strip()
        group = str(metadata.get("group") or "").strip()
        if not description or not group:
            raise ValueError(f"Settings 来源目录缺少说明或分组: {key}")
        core_specs[key] = SettingSpec(
            key=key,
            source="settings",
            group=group,
            label=description.split("；", 1)[0],
            description=description,
            value_annotation=model_field.annotation,
            declared_type=_format_declared_type(model_field.annotation),
            update_operations=("replace",),
            dependencies=_runtime_dependencies(key),
            apply_mode=(
                "restart_required"
                if group in _RESTART_REQUIRED_GROUPS
                else "immediate"
            ),
            unit=_runtime_unit(key),
            examples=_runtime_examples(
                model_field,
                sensitive=is_secret_setting_key(key),
            ),
            sensitive=is_secret_setting_key(key),
            source_file=str(metadata.get("source_file") or ""),
            source_line=int(metadata.get("source_line") or 0),
        )

    system_specs: dict[str, SettingSpec] = {}
    missing_contracts = set(SystemConfigKey) - SYSTEMCONFIG_CONTRACTS.keys()
    stale_contracts = SYSTEMCONFIG_CONTRACTS.keys() - set(SystemConfigKey)
    if missing_contracts or stale_contracts:
        raise ValueError(
            "SystemConfigKey 合同不完整: "
            f"missing={sorted(item.value for item in missing_contracts)}, "
            f"stale={sorted(item.value for item in stale_contracts)}"
        )
    for item in SystemConfigKey:
        source_metadata = system_catalog.get(item.value)
        if not isinstance(source_metadata, dict):
            raise ValueError(f"SystemConfigKey 来源目录缺少配置项: {item.value}")
        description = str(source_metadata.get("description") or "").strip()
        contract = SYSTEMCONFIG_CONTRACTS[item]
        system_specs[item.value] = SettingSpec(
            key=item.value,
            source="systemconfig",
            group=contract.group,
            label=description.split("；", 1)[0],
            description=description,
            value_annotation=contract.value_annotation,
            declared_type=_format_declared_type(contract.value_annotation),
            update_operations=contract.update_operations,
            generic_write_allowed=contract.generic_write_allowed,
            preferred_operation_ids=contract.preferred_operation_ids,
            dependencies=contract.dependencies,
            conflicts=contract.conflicts,
            apply_mode=contract.apply_mode,
            default_match_field=contract.default_match_field,
            unit=contract.unit,
            examples=contract.examples,
            sensitive=contract.sensitive,
            systemconfig_key=item,
            source_file=str(source_metadata.get("source_file") or ""),
            source_line=int(source_metadata.get("source_line") or 0),
        )
    return core_specs, system_specs


def build_value_schema(spec: SettingSpec) -> dict[str, Any]:
    """生成一个设置完整且自包含的 JSON Schema。"""
    if spec.source == "settings":
        properties = _runtime_settings_schema().get("properties", {})
        raw_schema = properties.get(spec.key)
        if not isinstance(raw_schema, dict):
            raise ValueError(f"Settings JSON Schema 缺少配置项: {spec.key}")
        schema = deepcopy(raw_schema)
        definitions = _runtime_settings_schema().get("$defs")
        if isinstance(definitions, dict):
            schema["$defs"] = deepcopy(definitions)
    else:
        schema = TypeAdapter(spec.value_annotation).json_schema()
    schema["description"] = spec.description
    if spec.sensitive:
        schema.pop("default", None)
    if spec.unit:
        schema.setdefault("x-unit", spec.unit)
    if spec.examples:
        schema.setdefault("examples", list(spec.examples))
    return schema


@lru_cache(maxsize=1)
def _runtime_settings_schema() -> dict[str, Any]:
    """缓存 Settings 生成的完整 JSON Schema，避免逐项重复建模。"""
    return cast(dict[str, Any], Settings.model_json_schema())


def validate_setting_value(spec: SettingSpec, value: Any) -> None:
    """按合同类型验证数据库设置的完整候选值。"""
    TypeAdapter(spec.value_annotation).validate_python(value)


CORE_SETTING_SPECS, SYSTEMCONFIG_SETTING_SPECS = build_setting_specs()
ALL_SETTING_SPECS = {**CORE_SETTING_SPECS, **SYSTEMCONFIG_SETTING_SPECS}
SETTING_GROUPS = frozenset(spec.group for spec in ALL_SETTING_SPECS.values())


__all__ = [
    "ALL_SETTING_SPECS",
    "CORE_SETTING_SPECS",
    "SETTING_GROUPS",
    "SYSTEMCONFIG_CONTRACTS",
    "SYSTEMCONFIG_SETTING_SPECS",
    "SettingSpec",
    "build_setting_specs",
    "build_value_schema",
    "validate_setting_value",
]
