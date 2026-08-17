"""插件运行时钩子契约。"""

from dataclasses import dataclass
from typing import Any

from app.foundation.reflection import ObjectUtils


@dataclass(frozen=True)
class PluginHookContract:
    """描述宿主识别一个插件钩子时必须保持的运行语义。"""

    name: str
    requires_enabled: bool = False
    isolates_errors: bool = True


PLUGIN_HOOK_CONTRACTS = {
    contract.name: contract
    for contract in (
        PluginHookContract("get_command", requires_enabled=True),
        PluginHookContract("get_api"),
        PluginHookContract("get_service", requires_enabled=True),
        PluginHookContract("get_module", requires_enabled=True),
        PluginHookContract("get_actions", requires_enabled=True),
        PluginHookContract("get_agent_tools", requires_enabled=True),
        PluginHookContract("get_auth_providers", requires_enabled=True),
        PluginHookContract("get_sidebar_nav", requires_enabled=True),
        PluginHookContract("get_dashboard", requires_enabled=True),
        PluginHookContract("get_dashboard_meta", requires_enabled=True),
        PluginHookContract("get_form"),
        PluginHookContract("get_page"),
        PluginHookContract("get_render_mode", isolates_errors=False),
    )
}


def supports_plugin_hook(plugin: Any, name: str) -> bool:
    """按旧插件的方法判定规则检查实例是否实现指定钩子。"""
    method = getattr(plugin, name, None)
    return bool(method and ObjectUtils.check_method(method))
