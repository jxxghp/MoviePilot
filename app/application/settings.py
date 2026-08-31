"""系统设置元数据、查询更新语义和敏感值脱敏能力。"""

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.application.configuration import (
    RuntimeSettingsService,
    SystemConfigService,
)
from app.application.plugin.runtime import plugin_system_config_mutation
from app.application.security.secrets import is_secret_setting_key
from app.runtime.config import Settings
from app.schemas.types import SystemConfigKey

SystemSettingPublisher = Callable[[Any, Any], Awaitable[None]]


@dataclass(frozen=True)
class SettingSpec:
    """描述一个可被 Agent 读写的系统设置项。"""

    key: str
    source: str
    group: str
    label: str
    systemconfig_key: Optional[SystemConfigKey] = None


SYSTEMCONFIG_SETTING_METADATA = {
    SystemConfigKey.Downloaders.value: {
        "group": "downloaders",
        "label": "下载器配置",
    },
    SystemConfigKey.MediaServers.value: {
        "group": "media_servers",
        "label": "媒体服务器配置",
    },
    SystemConfigKey.Notifications.value: {
        "group": "notifications",
        "label": "消息通知配置",
    },
    SystemConfigKey.NotificationSwitchs.value: {
        "group": "notification_switches",
        "label": "通知场景开关",
    },
    SystemConfigKey.Directories.value: {
        "group": "directories",
        "label": "目录配置",
    },
    SystemConfigKey.Storages.value: {
        "group": "storages",
        "label": "存储配置",
    },
    SystemConfigKey.IndexerSites.value: {
        "group": "search_sites",
        "label": "搜索站点范围",
    },
    SystemConfigKey.RssSites.value: {
        "group": "subscribe_sites",
        "label": "订阅站点范围",
    },
    SystemConfigKey.UserSiteAuthParams.value: {
        "group": "site_auth",
        "label": "站点认证参数",
    },
    SystemConfigKey.AIAgentConfig.value: {
        "group": "ai_agent",
        "label": "AI 智能体配置",
    },
    SystemConfigKey.AIAgentMcpServers.value: {
        "group": "ai_agent",
        "label": "AI 智能体外部 MCP 服务器",
    },
    SystemConfigKey.CustomIdentifiers.value: {
        "group": "custom_identifiers",
        "label": "自定义识别词",
    },
    SystemConfigKey.EpisodeFormatRuleTable.value: {
        "group": "transfer",
        "label": "集数定位规则词表",
    },
    SystemConfigKey.CustomReleaseGroups.value: {
        "group": "customization",
        "label": "自定义制作组/字幕组",
    },
    SystemConfigKey.Customization.value: {
        "group": "customization",
        "label": "自定义占位符",
    },
    SystemConfigKey.TransferExcludeWords.value: {
        "group": "transfer",
        "label": "整理屏蔽词",
    },
    SystemConfigKey.TorrentsPriority.value: {
        "group": "filter_rules",
        "label": "种子优先级规则",
    },
    SystemConfigKey.CustomFilterRules.value: {
        "group": "filter_rules",
        "label": "用户自定义规则",
    },
    SystemConfigKey.UserFilterRuleGroups.value: {
        "group": "filter_rules",
        "label": "用户规则组",
    },
    SystemConfigKey.SearchFilterRuleGroups.value: {
        "group": "filter_rules",
        "label": "搜索默认过滤规则组",
    },
    SystemConfigKey.SubscribeFilterRuleGroups.value: {
        "group": "filter_rules",
        "label": "订阅默认过滤规则组",
    },
    SystemConfigKey.BestVersionFilterRuleGroups.value: {
        "group": "filter_rules",
        "label": "洗版默认过滤规则组",
    },
    SystemConfigKey.SubscribeDefaultParams.value: {
        "group": "subscribe_defaults",
        "label": "订阅默认参数",
    },
    SystemConfigKey.DefaultMovieSubscribeConfig.value: {
        "group": "subscribe_defaults",
        "label": "默认电影订阅规则",
    },
    SystemConfigKey.DefaultTvSubscribeConfig.value: {
        "group": "subscribe_defaults",
        "label": "默认电视剧订阅规则",
    },
    SystemConfigKey.DefaultMusicSubscribeConfig.value: {
        "group": "subscribe_defaults",
        "label": "默认音乐订阅规则",
    },
    SystemConfigKey.UserInstalledPlugins.value: {
        "group": "plugins",
        "label": "已安装插件列表",
    },
    SystemConfigKey.PluginFolders.value: {
        "group": "plugins",
        "label": "插件文件夹分组配置",
    },
    SystemConfigKey.PluginInstallReport.value: {
        "group": "plugins",
        "label": "插件安装统计",
    },
    SystemConfigKey.NotificationSendTime.value: {
        "group": "notifications",
        "label": "通知发送时间",
    },
    SystemConfigKey.NotificationTemplates.value: {
        "group": "notifications",
        "label": "通知模板",
    },
    SystemConfigKey.ScrapingSwitchs.value: {
        "group": "scraping",
        "label": "刮削开关设置",
    },
    SystemConfigKey.FollowSubscribers.value: {
        "group": "subscribe_sites",
        "label": "Follow 订阅分享者",
    },
}


LIST_ITEM_MATCH_FIELD_DEFAULTS = {
    SystemConfigKey.Downloaders.value: "name",
    SystemConfigKey.MediaServers.value: "name",
    SystemConfigKey.Notifications.value: "name",
    SystemConfigKey.NotificationSwitchs.value: "type",
    SystemConfigKey.Directories.value: "name",
    SystemConfigKey.Storages.value: "name",
}


GROUP_ALIASES = {
    "all": "all",
    "全部": "all",
    "settings": "settings",
    "basic": "settings",
    "基础设置": "settings",
    "基础配置": "settings",
    "systemconfig": "systemconfig",
    "system_config": "systemconfig",
    "系统设置": "systemconfig",
    "系统配置": "systemconfig",
    "downloaders": "downloaders",
    "downloader": "downloaders",
    "下载器": "downloaders",
    "media_servers": "media_servers",
    "mediaservers": "media_servers",
    "media-servers": "media_servers",
    "媒体服务器": "media_servers",
    "notifications": "notifications",
    "notification": "notifications",
    "消息通知": "notifications",
    "通知": "notifications",
    "notification_switches": "notification_switches",
    "notification_switchs": "notification_switches",
    "通知开关": "notification_switches",
    "storages": "storages",
    "storage": "storages",
    "存储": "storages",
    "directories": "directories",
    "directory": "directories",
    "目录": "directories",
    "search_sites": "search_sites",
    "indexer_sites": "search_sites",
    "搜索站点": "search_sites",
    "subscribe_sites": "subscribe_sites",
    "rss_sites": "subscribe_sites",
    "订阅站点": "subscribe_sites",
    "site_auth": "site_auth",
    "site_auth_params": "site_auth",
    "站点认证": "site_auth",
    "ai_agent": "ai_agent",
    "agent": "ai_agent",
    "智能体": "ai_agent",
    "custom_identifiers": "custom_identifiers",
    "自定义识别词": "custom_identifiers",
    "filter_rules": "filter_rules",
    "过滤规则": "filter_rules",
    "subscribe_defaults": "subscribe_defaults",
    "订阅默认": "subscribe_defaults",
    "plugins": "plugins",
    "插件": "plugins",
    "customization": "customization",
    "自定义": "customization",
    "transfer": "transfer",
    "整理": "transfer",
    "scraping": "scraping",
    "刮削": "scraping",
    "misc": "misc",
    "其他": "misc",
}


# 这些前缀共同组成可启动、推理和扩展 AI Agent 的同一业务配置域。
AI_AGENT_CORE_SETTING_PREFIXES = (
    "AI_AGENT_",
    "LLM_",
    "AUDIO_INPUT_",
    "AUDIO_OUTPUT_",
    "AI_RECOMMEND_",
)


def _normalize_token(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _resolve_core_setting_group(key: str) -> str:
    """根据基础设置的业务归属返回 Agent 可查询的分类。"""

    if key.startswith(AI_AGENT_CORE_SETTING_PREFIXES):
        return "ai_agent"
    return "settings"


def _build_specs() -> tuple[dict[str, SettingSpec], dict[str, SettingSpec]]:
    core_specs = {
        key: SettingSpec(
            key=key,
            source="settings",
            group=_resolve_core_setting_group(key),
            label=key,
        )
        for key in Settings.model_fields.keys()
    }
    system_specs = {}
    for item in SystemConfigKey:
        metadata = SYSTEMCONFIG_SETTING_METADATA.get(item.value, {})
        system_specs[item.value] = SettingSpec(
            key=item.value,
            source="systemconfig",
            group=metadata.get("group", "misc"),
            label=metadata.get("label", item.value),
            systemconfig_key=item,
        )
    return core_specs, system_specs


CORE_SETTING_SPECS, SYSTEMCONFIG_SETTING_SPECS = _build_specs()
ALL_SETTING_SPECS = {**CORE_SETTING_SPECS, **SYSTEMCONFIG_SETTING_SPECS}


SETTING_KEY_ALIASES = {}
for key in CORE_SETTING_SPECS:
    SETTING_KEY_ALIASES[_normalize_token(key)] = key
for item in SystemConfigKey:
    SETTING_KEY_ALIASES[_normalize_token(item.value)] = item.value
    SETTING_KEY_ALIASES[_normalize_token(item.name)] = item.value

SINGLE_KEY_GROUP_ALIASES = {
    _normalize_token(alias): next(
        (spec.key for spec in ALL_SETTING_SPECS.values() if spec.group == canonical_group),
        None,
    )
    for alias, canonical_group in GROUP_ALIASES.items()
    if canonical_group not in {"all", "settings", "systemconfig"}
    and len([spec.key for spec in ALL_SETTING_SPECS.values() if spec.group == canonical_group]) == 1
}


def normalize_group(group: Optional[str]) -> str:
    if not group:
        return "all"
    normalized = GROUP_ALIASES.get(_normalize_token(group))
    if not normalized:
        raise ValueError(
            "group 不支持，支持值包括 all/settings/systemconfig 以及"
            " downloaders、media_servers、notifications、storages、directories、"
            "search_sites、subscribe_sites、site_auth、ai_agent 等分类别名"
        )
    return normalized


def resolve_setting_spec(setting_key: Optional[str]) -> Optional[SettingSpec]:
    """把精确键名、枚举名或单键分组别名解析为统一的设置定义。"""

    if not setting_key:
        return None

    normalized = _normalize_token(setting_key)
    resolved_key = SETTING_KEY_ALIASES.get(normalized) or SINGLE_KEY_GROUP_ALIASES.get(normalized)
    if not resolved_key:
        return None
    return ALL_SETTING_SPECS.get(resolved_key)


def list_setting_specs(group: Optional[str] = "all", keyword: Optional[str] = None) -> list[SettingSpec]:
    """按分组和关键字筛选可查询的设置项。"""

    normalized_group = normalize_group(group)
    if normalized_group == "all":
        specs = list(ALL_SETTING_SPECS.values())
    elif normalized_group == "settings":
        specs = list(CORE_SETTING_SPECS.values())
    elif normalized_group == "systemconfig":
        specs = list(SYSTEMCONFIG_SETTING_SPECS.values())
    else:
        specs = [spec for spec in ALL_SETTING_SPECS.values() if spec.group == normalized_group]

    if keyword:
        normalized_keyword = _normalize_token(keyword)
        specs = [
            spec
            for spec in specs
            if normalized_keyword in _normalize_token(spec.key)
            or normalized_keyword in _normalize_token(spec.group)
            or normalized_keyword in _normalize_token(spec.label)
        ]

    return sorted(specs, key=lambda spec: (spec.source, spec.group, spec.key))


def get_default_list_match_field(setting_key: str) -> Optional[str]:
    return LIST_ITEM_MATCH_FIELD_DEFAULTS.get(setting_key)


def redact_secret_value(value: Any, *, redact_scalar: bool = False) -> Any:
    """递归脱敏配置值中的密钥、Cookie、Token 等敏感字段。"""
    if isinstance(value, dict):
        return {
            key: "***" if is_secret_setting_key(str(key)) else redact_secret_value(item, redact_scalar=redact_scalar)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_value(item, redact_scalar=redact_scalar) for item in value]
    if isinstance(value, str):
        return "***" if value and redact_scalar else value
    return value


def should_redact_setting(spec: SettingSpec, value: Any) -> bool:
    """判断某项设置在默认查询响应中是否需要脱敏。"""
    if is_secret_setting_key(spec.key):
        return True
    if isinstance(value, dict):
        return any(is_secret_setting_key(str(key)) for key in value.keys())
    if isinstance(value, list):
        return any(should_redact_setting(spec, item) for item in value if isinstance(item, dict))
    return False


class SystemSettingsService:
    """提供 Agent 与管理 API 共用的受控系统设置查询和更新。"""

    def __init__(
        self,
        runtime_settings: RuntimeSettingsService,
        system_config: SystemConfigService,
        publish_config_changed: SystemSettingPublisher,
    ) -> None:
        """注入部署设置、持久化配置和配置事件发布端口。"""
        self._runtime_settings = runtime_settings
        self._system_config = system_config
        self._publish_config_changed = publish_config_changed

    def _load(self, spec: SettingSpec) -> Any:
        """按设置来源读取当前值。"""
        if spec.source == "settings":
            return self._runtime_settings.get(spec.key)
        return self._system_config.get(spec.systemconfig_key)

    @staticmethod
    def _summarize(value: Any, *, redacted: bool = False) -> dict[str, Any]:
        """生成有界值摘要，避免配置列表和字典挤占响应上下文。"""
        summary: dict[str, Any] = {
            "has_value": value is not None,
            "value_type": type(value).__name__,
            "redacted": redacted,
        }
        if isinstance(value, list):
            summary["item_count"] = len(value)
            if value:
                summary["item_type"] = type(value[0]).__name__
        elif isinstance(value, dict):
            keys = list(value)
            summary["item_count"] = len(keys)
            summary["keys_preview"] = keys[:10]
            summary["keys_truncated"] = len(keys) > 10
        elif isinstance(value, str):
            summary["length"] = len(value)
            summary["value_preview"] = value[:200]
            summary["value_truncated"] = len(value) > 200
        elif value is not None:
            summary["value_preview"] = value
        return summary

    def query(
        self,
        *,
        setting_key: Optional[str] = None,
        group: Optional[str] = "all",
        keyword: Optional[str] = None,
        include_values: Optional[bool] = None,
        show_secrets: bool = False,
    ) -> dict[str, Any]:
        """查询登记设置，并在未授权明文读取时递归脱敏。"""
        if setting_key:
            spec = resolve_setting_spec(setting_key)
            if spec is None:
                raise ValueError(f"系统设置项 '{setting_key}' 不存在")
            specs = [spec]
        else:
            specs = list_setting_specs(group=group, keyword=keyword)
            if not specs:
                raise ValueError("没有找到匹配的系统设置项")
        should_include_values = include_values if include_values is not None else len(specs) == 1
        payload = []
        for spec in specs:
            value = self._load(spec)
            redacted = should_redact_setting(spec, value) and not show_secrets
            response_value = (
                redact_secret_value(
                    value,
                    redact_scalar=is_secret_setting_key(spec.key),
                )
                if redacted
                else value
            )
            item = {
                "setting_key": spec.key,
                "source": spec.source,
                "group": spec.group,
                "label": spec.label,
                **self._summarize(response_value, redacted=redacted),
            }
            if should_include_values:
                item["value"] = response_value
            payload.append(item)
        return {
            "matched_count": len(payload),
            "include_values": should_include_values,
            "show_secrets": show_secrets,
            "settings": payload,
        }

    @staticmethod
    def _normalize_systemconfig_value(value: Any) -> Any:
        """将只含空项的列表折叠为空配置。"""
        if isinstance(value, list):
            filtered = [item for item in value if item is not None]
            return filtered or None
        return value

    @staticmethod
    def _resolve_list_match(
        spec: SettingSpec,
        operation: str,
        value: Any,
        match_field: Optional[str],
        match_value: Any,
    ) -> tuple[Optional[str], Any]:
        """解析列表项修改使用的稳定匹配字段和值。"""
        resolved_field = match_field or get_default_list_match_field(spec.key)
        resolved_value = match_value
        if isinstance(value, dict):
            if not resolved_field:
                raise ValueError(f"{operation} 需要提供 match_field，或使用带默认匹配字段的系统配置项")
            if resolved_value is None:
                resolved_value = value.get(resolved_field)
            if resolved_value is None:
                raise ValueError(f"{operation} 缺少匹配值，请在 value.{resolved_field} 或 match_value 中提供")
        elif resolved_value is None:
            resolved_value = value
        return resolved_field, resolved_value

    @classmethod
    def _prepare_next_value(
        cls,
        spec: SettingSpec,
        current_value: Any,
        value: Any,
        operation: str,
        remove_keys: Optional[list[str]],
        match_field: Optional[str],
        match_value: Any,
    ) -> Any:
        """按显式更新语义构造下一配置值。"""
        if operation == "replace":
            return value
        if operation == "merge_dict":
            if current_value is not None and not isinstance(current_value, dict):
                raise ValueError("merge_dict 仅支持当前值为 dict 的设置项")
            if value is not None and not isinstance(value, dict):
                raise ValueError("merge_dict 的 value 必须是 dict 或 null")
            next_value = dict(current_value or {})
            next_value.update(value or {})
            for key in remove_keys or []:
                next_value.pop(key, None)
            return next_value
        if operation not in {"upsert_list_item", "remove_list_item"}:
            raise ValueError(f"不支持的操作: {operation}")
        if current_value is not None and not isinstance(current_value, list):
            raise ValueError(f"{operation} 仅支持当前值为 list 的设置项")
        next_items = list(copy.deepcopy(current_value or []))
        resolved_field, resolved_value = cls._resolve_list_match(spec, operation, value, match_field, match_value)
        if operation == "upsert_list_item":
            if value is None:
                raise ValueError("upsert_list_item 必须提供 value")
            for index, item in enumerate(next_items):
                matched = (
                    isinstance(item, dict) and resolved_field and item.get(resolved_field) == resolved_value
                ) or (not resolved_field and item == resolved_value)
                if matched:
                    next_items[index] = value
                    break
            else:
                next_items.append(value)
            return next_items
        return [
            item
            for item in next_items
            if not (isinstance(item, dict) and resolved_field and item.get(resolved_field) == resolved_value)
            and not (not resolved_field and item == resolved_value)
        ]

    async def update(
        self,
        *,
        setting_key: str,
        value: Any = None,
        operation: str = "replace",
        remove_keys: Optional[list[str]] = None,
        match_field: Optional[str] = None,
        match_value: Any = None,
    ) -> dict[str, Any]:
        """更新登记设置并发布统一配置变更事件。"""
        spec = resolve_setting_spec(setting_key)
        if spec is None:
            raise ValueError(f"系统设置项 '{setting_key}' 不存在")
        mutation_key = spec.systemconfig_key if spec.source == "systemconfig" else None
        with plugin_system_config_mutation(mutation_key):
            previous_value = self._load(spec)
            next_value = self._prepare_next_value(
                spec,
                previous_value,
                value,
                operation,
                remove_keys,
                match_field,
                match_value,
            )
            message = ""
            event_value = next_value
            if spec.source == "settings":
                success, message = self._runtime_settings.update(spec.key, next_value)
                if success is False:
                    raise ValueError(message or f"更新设置 {spec.key} 失败")
                changed = success is True
            else:
                event_value = self._normalize_systemconfig_value(next_value)
                changed = bool(
                    await self._system_config.async_set(
                        spec.systemconfig_key,
                        event_value,
                    )
                )
            if changed:
                await self._publish_config_changed(spec.key, event_value)
            saved_value = self._load(spec)
            redact_values = should_redact_setting(spec, previous_value) or should_redact_setting(spec, saved_value)
            return {
                "message": message or (f"系统设置 {spec.key} 已更新" if changed else "配置值未发生变化"),
                "changed": changed,
                "operation": operation,
                "setting": {
                    "setting_key": spec.key,
                    "source": spec.source,
                    "group": spec.group,
                    "label": spec.label,
                },
                "values_redacted": redact_values,
                "previous_value": (
                    redact_secret_value(
                        previous_value,
                        redact_scalar=is_secret_setting_key(spec.key),
                    )
                    if redact_values
                    else previous_value
                ),
                "saved_value": (
                    redact_secret_value(
                        saved_value,
                        redact_scalar=is_secret_setting_key(spec.key),
                    )
                    if redact_values
                    else saved_value
                ),
            }
