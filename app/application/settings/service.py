"""系统设置元数据、查询更新语义和敏感值脱敏能力。"""

import copy
import hashlib
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any, ContextManager, Optional, cast

from app.application.configuration import (
    RuntimeSettingsService,
    SystemConfigService,
)
from app.application.security.secrets import is_secret_setting_key
from app.application.settings.contract import (
    ALL_SETTING_SPECS,
    CORE_SETTING_SPECS,
    SETTING_GROUPS,
    SYSTEMCONFIG_SETTING_SPECS,
    SettingSpec,
    build_value_schema,
    validate_setting_value,
)
from app.schemas.types import SystemConfigKey

SystemSettingPublisher = Callable[[Any, Any], Awaitable[None]]


class SystemSettingConflictError(ValueError):
    """表示条件更新所依据的系统配置快照已经过期。"""


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
    "custom_identifiers": "recognition_words",
    "自定义识别词": "recognition_words",
    "filter_rules": "filter_rules",
    "过滤规则": "filter_rules",
    "subscribe_defaults": "subscribe_defaults",
    "订阅默认": "subscribe_defaults",
    "plugins": "plugins",
    "插件": "plugins",
    "customization": "recognition_words",
    "自定义": "recognition_words",
    "transfer": "transfer",
    "整理": "transfer",
    "scraping": "scraping",
    "刮削": "scraping",
    "misc": "misc",
    "其他": "misc",
}

_RUNTIME_SETTINGS_UPDATE_LOCK = threading.RLock()


def _normalize_token(value: str) -> str:
    """把键名或别名转换为大小写和连字符无关的比较形式。"""
    return str(value).strip().lower().replace("-", "_")


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
    """解析内置分类别名或来源目录中的精确分类名。"""
    if not group:
        return "all"
    token = _normalize_token(group)
    normalized = GROUP_ALIASES.get(token) or (token if token in SETTING_GROUPS else None)
    if not normalized:
        raise ValueError(
            "group 不支持，支持 all/settings/systemconfig 或目录返回的分类："
            f"{', '.join(sorted(SETTING_GROUPS))}"
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
            or normalized_keyword in _normalize_token(spec.description)
        ]

    return sorted(specs, key=lambda spec: (spec.source, spec.group, spec.key))


def get_default_list_match_field(setting_key: str) -> Optional[str]:
    """返回合同声明的列表项默认匹配字段。"""
    spec = ALL_SETTING_SPECS.get(setting_key)
    return spec.default_match_field if spec else None


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


def _contains_secret_field(value: Any) -> bool:
    """递归判断一个配置值是否包含命名为敏感字段的成员。"""
    if isinstance(value, dict):
        return any(
            is_secret_setting_key(str(key)) or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


def should_redact_setting(spec: SettingSpec, value: Any) -> bool:
    """判断某项设置在默认查询响应中是否需要脱敏。"""
    return spec.sensitive or is_secret_setting_key(spec.key) or _contains_secret_field(value)


def redact_setting_value(spec: SettingSpec, value: Any) -> Any:
    """按设置结构脱敏，保留复杂配置中的非敏感匹配字段。"""
    if not should_redact_setting(spec, value):
        return value
    redact_scalar = is_secret_setting_key(spec.key) or not isinstance(value, (dict, list))
    return redact_secret_value(value, redact_scalar=redact_scalar)


def build_setting_revision(value: Any) -> str:
    """为当前完整配置值生成稳定且不暴露原文的并发修订标识。"""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _system_config_mutation(key: Any) -> ContextManager[None]:
    """解析配置 mutation 上下文，并保留旧 settings 模块的替换兼容性。"""
    from app.application import settings as settings_facade

    mutation = getattr(settings_facade, "plugin_system_config_mutation")
    return cast(ContextManager[None], mutation(key))


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    """判断一个自包含 JSON Schema 是否允许 null。"""
    schema_type = schema.get("type")
    if schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type):
        return True
    return any(
        isinstance(option, dict) and _schema_allows_null(option)
        for keyword in ("anyOf", "oneOf")
        for option in schema.get(keyword, [])
    )


def _summarize_setting_value(value: Any, *, redacted: bool = False) -> dict[str, Any]:
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


def _catalog_item(spec: SettingSpec) -> dict[str, Any]:
    """投影适合批量扫描的轻量设置合同摘要。"""
    return {
        "setting_key": spec.key,
        "source": spec.source,
        "group": spec.group,
        "label": spec.label,
        "description": spec.description,
        "declared_type": spec.declared_type,
        "sensitive": spec.sensitive,
        "generic_write_allowed": spec.generic_write_allowed,
        "update_operations": list(spec.update_operations),
        "default_match_field": spec.default_match_field,
        "preferred_operation_ids": list(spec.preferred_operation_ids),
        "dependencies": list(spec.dependencies),
        "conflicts": list(spec.conflicts),
        "apply_mode": spec.apply_mode,
        "unit": spec.unit,
        "examples": list(spec.examples),
    }


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
    def _definition(
        spec: SettingSpec,
        value: Any,
        *,
        sensitive: bool,
        include_schema: bool,
    ) -> dict[str, Any]:
        """返回由静态合同声明的设置结构、写入边界和来源信息。"""
        schema = build_value_schema(spec)
        definition = {
            "description": spec.description,
            "declared_type": spec.declared_type,
            "default": schema.get("default"),
            "value_shape": type(value).__name__ if value is not None else "null",
            "nullable": _schema_allows_null(schema),
            "sensitive": sensitive,
            "generic_write_allowed": spec.generic_write_allowed,
            "update_operations": list(spec.update_operations),
            "default_match_field": spec.default_match_field,
            "preferred_operation_ids": list(spec.preferred_operation_ids),
            "dependencies": list(spec.dependencies),
            "conflicts": list(spec.conflicts),
            "apply_mode": spec.apply_mode,
            "unit": spec.unit,
            "examples": list(spec.examples),
            "persistence": (
                "app.env"
                if spec.source == "settings"
                else "database:systemconfig"
            ),
            "source": {
                "file": spec.source_file,
                "line": spec.source_line,
            },
        }
        if include_schema:
            definition["value_schema"] = schema
        return definition

    def catalog(
        self,
        *,
        group: Optional[str] = "all",
        keyword: Optional[str] = None,
        source: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页列出轻量设置合同，不读取或返回当前配置值。"""
        if source not in {None, "settings", "systemconfig"}:
            raise ValueError("source 仅支持 settings 或 systemconfig")
        if offset < 0:
            raise ValueError("offset 必须大于等于 0")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        specs = list_setting_specs(group=group, keyword=keyword)
        if source:
            specs = [spec for spec in specs if spec.source == source]
        page = specs[offset : offset + limit]
        return {
            "matched_count": len(specs),
            "returned_count": len(page),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(specs),
            "settings": [_catalog_item(spec) for spec in page],
        }

    def describe(
        self,
        *,
        setting_key: str,
        include_value: bool = True,
        show_secrets: bool = False,
    ) -> dict[str, Any]:
        """返回一个设置的完整合同、当前 revision 和可选脱敏值。"""
        spec = resolve_setting_spec(setting_key)
        if spec is None:
            raise ValueError(f"系统设置项 '{setting_key}' 不存在")
        value = self._load(spec)
        sensitive = should_redact_setting(spec, value)
        redacted = sensitive and not show_secrets
        response_value = (
            redact_setting_value(spec, value)
            if redacted
            else value
        )
        item = {
            "setting_key": spec.key,
            "source": spec.source,
            "group": spec.group,
            "label": spec.label,
            "revision": build_setting_revision(value),
            "definition": self._definition(
                spec,
                value,
                sensitive=sensitive,
                include_schema=True,
            ),
            **_summarize_setting_value(response_value, redacted=redacted),
        }
        if include_value:
            item["value"] = response_value
        return item

    def query(
        self,
        *,
        setting_key: Optional[str] = None,
        group: Optional[str] = "all",
        keyword: Optional[str] = None,
        include_values: Optional[bool] = None,
        show_secrets: bool = False,
    ) -> dict[str, Any]:
        """保留旧查询入口，并投影增强后的合同和 revision。"""
        if setting_key:
            should_include_values = include_values if include_values is not None else True
            item = self.describe(
                setting_key=setting_key,
                include_value=should_include_values,
                show_secrets=show_secrets,
            )
            return {
                "matched_count": 1,
                "include_values": should_include_values,
                "show_secrets": show_secrets,
                "settings": [item],
            }
        specs = list_setting_specs(group=group, keyword=keyword)
        if not specs:
            raise ValueError("没有找到匹配的系统设置项")
        should_include_values = include_values if include_values is not None else False
        payload = []
        for spec in specs:
            value = self._load(spec)
            sensitive = should_redact_setting(spec, value)
            redacted = sensitive and not show_secrets
            response_value = (
                redact_setting_value(spec, value)
                if redacted
                else value
            )
            item = {
                "setting_key": spec.key,
                "source": spec.source,
                "group": spec.group,
                "label": spec.label,
                "revision": build_setting_revision(value),
                "definition": self._definition(
                    spec,
                    value,
                    sensitive=sensitive,
                    include_schema=False,
                ),
                **_summarize_setting_value(response_value, redacted=redacted),
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

    @classmethod
    def _normalize_comparison_value(cls, spec: SettingSpec, value: Any) -> Any:
        """按专用 API 的公开投影规范化条件更新比较值。"""
        normalized = cls._normalize_systemconfig_value(value)
        if spec.systemconfig_key == SystemConfigKey.CustomIdentifiers and isinstance(normalized, list):
            identifiers = [item for item in normalized if isinstance(item, str)]
            return identifiers or None
        return normalized

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

    @staticmethod
    def _assert_update_allowed(
        spec: SettingSpec,
        operation: str,
        *,
        allow_managed_write: bool,
    ) -> None:
        """按合同拒绝通用写入和未声明的更新语义。"""
        if not spec.generic_write_allowed and not allow_managed_write:
            preferred = ", ".join(spec.preferred_operation_ids)
            if preferred:
                raise ValueError(
                    f"系统设置 {spec.key} 禁止通用写入，请改用专用 operation: {preferred}"
                )
            raise ValueError(f"系统设置 {spec.key} 是内部状态，禁止通过通用设置接口写入")
        allowed_operations = spec.update_operations
        if allow_managed_write and not allowed_operations:
            allowed_operations = ("replace",)
        if operation not in allowed_operations:
            supported = ", ".join(allowed_operations) or "无"
            raise ValueError(
                f"系统设置 {spec.key} 不允许 {operation}，支持的更新操作: {supported}"
            )

    def _prepare_systemconfig_value(
        self,
        spec: SettingSpec,
        current_value: Any,
        value: Any,
        operation: str,
        remove_keys: Optional[list[str]],
        match_field: Optional[str],
        match_value: Any,
    ) -> Any:
        """构造、规范化并按完整合同验证数据库设置候选值。"""
        next_value = self._prepare_next_value(
            spec,
            current_value,
            value,
            operation,
            remove_keys,
            match_field,
            match_value,
        )
        normalized_value = self._normalize_systemconfig_value(next_value)
        validate_setting_value(spec, normalized_value)
        return normalized_value

    async def update(
        self,
        *,
        setting_key: str,
        value: Any = None,
        operation: str = "replace",
        remove_keys: Optional[list[str]] = None,
        match_field: Optional[str] = None,
        match_value: Any = None,
        expected_revision: Optional[str] = None,
        expected_value: Any = None,
        enforce_expected_value: bool = False,
        allow_managed_write: bool = False,
    ) -> dict[str, Any]:
        """按合同和 revision 原子更新设置，并发布统一配置变更事件。"""
        spec = resolve_setting_spec(setting_key)
        if spec is None:
            raise ValueError(f"系统设置项 '{setting_key}' 不存在")
        if enforce_expected_value and spec.source != "systemconfig":
            raise ValueError("条件更新仅支持数据库系统配置")
        self._assert_update_allowed(
            spec,
            operation,
            allow_managed_write=allow_managed_write or enforce_expected_value,
        )
        mutation_key = spec.systemconfig_key if spec.source == "systemconfig" else None
        with _system_config_mutation(mutation_key):
            message = ""
            if spec.source == "settings":
                with _RUNTIME_SETTINGS_UPDATE_LOCK:
                    previous_value = self._load(spec)
                    previous_revision = build_setting_revision(previous_value)
                    if expected_revision is not None and expected_revision != previous_revision:
                        raise SystemSettingConflictError(
                            f"系统设置 {spec.key} 的 expected_revision 已过期，请重新读取后再保存"
                        )
                    next_value = self._prepare_next_value(
                        spec,
                        previous_value,
                        value,
                        operation,
                        remove_keys,
                        match_field,
                        match_value,
                    )
                    success, message = self._runtime_settings.update(spec.key, next_value)
                    if success is False:
                        raise ValueError(message or f"更新设置 {spec.key} 失败")
                    changed = success is True
                    event_value = self._load(spec)
            elif expected_revision is not None or enforce_expected_value:
                normalized_expected = (
                    self._normalize_comparison_value(spec, expected_value)
                    if enforce_expected_value
                    else None
                )

                def mutate(current_value: Any) -> tuple[tuple[Any, Any, bool], Any]:
                    """在配置写锁内校验 revision 或旧值并构造合法结果。"""
                    if (
                        expected_revision is not None
                        and build_setting_revision(current_value) != expected_revision
                    ):
                        raise SystemSettingConflictError(
                            f"系统设置 {spec.key} 的 expected_revision 已过期，请重新读取后再保存"
                        )
                    normalized_current = self._normalize_comparison_value(spec, current_value)
                    if enforce_expected_value and normalized_current != normalized_expected:
                        raise SystemSettingConflictError(
                            f"系统设置 {spec.key} 已被其他会话更新，请重新加载后再保存"
                        )
                    normalized_next = self._prepare_systemconfig_value(
                        spec,
                        current_value,
                        value,
                        operation,
                        remove_keys,
                        match_field,
                        match_value,
                    )
                    return (
                        current_value,
                        normalized_next,
                        current_value != normalized_next,
                    ), normalized_next

                previous_value, event_value, changed = (
                    await self._system_config.async_update_atomically(
                        spec.systemconfig_key,
                        mutate,
                    )
                )
            else:
                previous_value = self._load(spec)
                event_value = self._prepare_systemconfig_value(
                    spec,
                    previous_value,
                    value,
                    operation,
                    remove_keys,
                    match_field,
                    match_value,
                )
                write_result = (
                    await self._system_config.async_set_with_normalized_value(
                        spec.systemconfig_key,
                        event_value,
                    )
                )
                event_value = write_result.normalized_value
                changed = bool(write_result.changed)
            if changed:
                await self._publish_config_changed(spec.key, event_value)
            saved_value = self._load(spec)
            previous_revision = build_setting_revision(previous_value)
            saved_revision = build_setting_revision(saved_value)
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
                "previous_revision": previous_revision,
                "revision": saved_revision,
                "values_redacted": redact_values,
                "previous_value": (
                    redact_setting_value(spec, previous_value)
                    if redact_values
                    else previous_value
                ),
                "saved_value": (
                    redact_setting_value(spec, saved_value)
                    if redact_values
                    else saved_value
                ),
            }
