"""插件市场技术依赖的唯一启动组合 owner。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.adapters.external.plugin.client import (
    PluginMarketClient,
    PluginMarketTransport,
    PluginPackageSourceClient,
)
from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.adapters.system.plugin.health import PluginRuntimeHealth
from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.compat.readiness import plugin_multi_version_blockers
from app.runtime.extensions.plugin.version import (
    PLUGIN_FALLBACK_VERSION,
    read_declared_plugin_version,
)
from app.runtime.settings import get_runtime_setting


def _reject_incompatible_plugin_version_switch(
    plugin_id: str,
    plugin_dir: Path,
    source_dir: Path,
) -> Optional[str]:
    """判定插件从已装版本切换到另一版本能否在安装期被接受。

    只在声明版本号确实发生变化时才检查——同版本重新同步是开发闭环的日常操作，
    不是在装另一个版本，不需要为此扫描全部源码。命中自引用绝对导入或宿主共享
    声明基类建模时拒绝：这两类写法在真正的多版本并存下必然失败，把故障从运行
    期提前到安装时。这个组合只能落在组合根——版本目录布局属于运行时扩展包，
    写法体检属于兼容层静态扫描，两者都不允许被适配器或运行时扩展包本身引用。

    :param plugin_id: 插件ID
    :param plugin_dir: 插件当前运行目录；未声明源码（尚未安装）时不检查
    :param source_dir: 待安装的插件源码目录
    :return: 拒绝说明；无需拒绝时为 None
    """
    installed_init = plugin_dir / "__init__.py"
    if not installed_init.is_file():
        return None
    installed_version = read_declared_plugin_version(installed_init) or PLUGIN_FALLBACK_VERSION
    incoming_version = (
        read_declared_plugin_version(source_dir / "__init__.py") or PLUGIN_FALLBACK_VERSION
    )
    if installed_version == incoming_version:
        return None
    blockers = plugin_multi_version_blockers(plugin_id.lower(), [plugin_dir, source_dir])
    if not blockers:
        return None
    return (
        f"插件 {plugin_id} 的写法不支持多版本并存，拒绝从 {installed_version} 版本切换到 "
        f"{incoming_version} 版本：" + "；".join(blockers)
    )


@dataclass(frozen=True, slots=True)
class PluginMarketComposition:
    """保存插件市场相关 Transport、Client、Package 和 Dependency owner。"""

    transport: PluginMarketTransport
    client: PluginMarketClient
    package: PluginPackageManager
    health: PluginRuntimeHealth
    dependency: PluginDependencyInstaller


_market_client: Optional[PluginMarketClient] = None


def compose_plugin_market(
    *,
    installed_plugins_provider: Callable[[], list[str]],
) -> PluginMarketComposition:
    """一次性构造插件市场技术依赖，供同一 lifespan 内所有用例复用。"""
    global _market_client
    root_path = Path(get_runtime_setting("ROOT_PATH"))
    plugin_root = root_path / "app" / "plugins"
    transport = PluginMarketTransport.get_existing_instance() or PluginMarketTransport()
    client = PluginMarketClient(transport)
    health = PluginRuntimeHealth()
    composition = PluginMarketComposition(
        transport=transport,
        client=client,
        package=PluginPackageManager(
            source=PluginPackageSourceClient(transport),
            plugin_root=plugin_root,
            version_switch_guard=_reject_incompatible_plugin_version_switch,
        ),
        dependency=PluginDependencyInstaller(
            health,
            installed_plugins_provider=installed_plugins_provider,
            plugin_dir=plugin_root,
        ),
        health=health,
    )
    _market_client = client
    return composition


def get_composed_plugin_market_client() -> PluginMarketClient:
    """返回当前 lifespan 由组合根构造的唯一插件市场 Client。"""
    if _market_client is None:
        raise RuntimeError("插件市场 Client 尚未由启动组合根装配")
    return _market_client


def reset_plugin_market_composition() -> None:
    """撤销当前 lifespan 的插件市场 Client 投影。"""
    global _market_client
    _market_client = None
