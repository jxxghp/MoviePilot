"""系统设置工具共用的键解析与分组元数据。"""

from dataclasses import dataclass
from typing import Optional

from app.core.config import Settings
from app.schemas.types import SystemConfigKey


@dataclass(frozen=True)
class SettingSpec:
    """描述一个可被 Agent 读写的系统设置项。"""

    key: str
    source: str
    group: str
    label: str


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


def _normalize_token(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _build_specs() -> tuple[dict[str, SettingSpec], dict[str, SettingSpec]]:
    core_specs = {
        key: SettingSpec(key=key, source="settings", group="settings", label=key)
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
        (
            spec.key
            for spec in SYSTEMCONFIG_SETTING_SPECS.values()
            if spec.group == canonical_group
        ),
        None,
    )
    for alias, canonical_group in GROUP_ALIASES.items()
    if canonical_group not in {"all", "settings", "systemconfig"}
    and len(
        [
            spec.key
            for spec in SYSTEMCONFIG_SETTING_SPECS.values()
            if spec.group == canonical_group
        ]
    )
    == 1
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
    resolved_key = SETTING_KEY_ALIASES.get(normalized) or SINGLE_KEY_GROUP_ALIASES.get(
        normalized
    )
    if not resolved_key:
        return None
    return ALL_SETTING_SPECS.get(resolved_key)


def list_setting_specs(
    group: Optional[str] = "all", keyword: Optional[str] = None
) -> list[SettingSpec]:
    """按分组和关键字筛选可查询的设置项。"""

    normalized_group = normalize_group(group)
    if normalized_group == "all":
        specs = list(ALL_SETTING_SPECS.values())
    elif normalized_group == "settings":
        specs = list(CORE_SETTING_SPECS.values())
    elif normalized_group == "systemconfig":
        specs = list(SYSTEMCONFIG_SETTING_SPECS.values())
    else:
        specs = [
            spec
            for spec in SYSTEMCONFIG_SETTING_SPECS.values()
            if spec.group == normalized_group
        ]

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
