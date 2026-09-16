"""系统设置应用服务和静态合同的兼容懒加载门面。"""

from importlib import import_module as _import_module
from typing import Any as _Any

_EXPORTS = {
    "ALL_SETTING_SPECS": ("app.application.settings.contract", "ALL_SETTING_SPECS"),
    "CORE_SETTING_SPECS": ("app.application.settings.contract", "CORE_SETTING_SPECS"),
    "GROUP_ALIASES": ("app.application.settings.service", "GROUP_ALIASES"),
    "SETTING_GROUPS": ("app.application.settings.contract", "SETTING_GROUPS"),
    "SETTING_KEY_ALIASES": ("app.application.settings.service", "SETTING_KEY_ALIASES"),
    "SINGLE_KEY_GROUP_ALIASES": (
        "app.application.settings.service",
        "SINGLE_KEY_GROUP_ALIASES",
    ),
    "SYSTEMCONFIG_CONTRACTS": (
        "app.application.settings.contract",
        "SYSTEMCONFIG_CONTRACTS",
    ),
    "SYSTEMCONFIG_SETTING_SPECS": (
        "app.application.settings.contract",
        "SYSTEMCONFIG_SETTING_SPECS",
    ),
    "SettingSpec": ("app.application.settings.contract", "SettingSpec"),
    "SystemConfigKey": ("app.application.settings.service", "SystemConfigKey"),
    "SystemSettingConflictError": (
        "app.application.settings.service",
        "SystemSettingConflictError",
    ),
    "SystemSettingsService": (
        "app.application.settings.service",
        "SystemSettingsService",
    ),
    "build_setting_revision": (
        "app.application.settings.service",
        "build_setting_revision",
    ),
    "build_setting_specs": (
        "app.application.settings.contract",
        "build_setting_specs",
    ),
    "build_value_schema": (
        "app.application.settings.contract",
        "build_value_schema",
    ),
    "get_default_list_match_field": (
        "app.application.settings.service",
        "get_default_list_match_field",
    ),
    "list_setting_specs": (
        "app.application.settings.service",
        "list_setting_specs",
    ),
    "normalize_group": ("app.application.settings.service", "normalize_group"),
    "plugin_system_config_mutation": (
        "app.application.plugin.runtime",
        "plugin_system_config_mutation",
    ),
    "redact_secret_value": (
        "app.application.settings.service",
        "redact_secret_value",
    ),
    "redact_setting_value": (
        "app.application.settings.service",
        "redact_setting_value",
    ),
    "resolve_setting_spec": (
        "app.application.settings.service",
        "resolve_setting_spec",
    ),
    "should_redact_setting": (
        "app.application.settings.service",
        "should_redact_setting",
    ),
    "validate_setting_value": (
        "app.application.settings.contract",
        "validate_setting_value",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> _Any:
    """按公开清单延迟解析设置服务和合同符号。"""
    contract = _EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(_import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """返回兼容门面公开的设置符号清单。"""
    return sorted({*globals(), *_EXPORTS})
