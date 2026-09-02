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
from app.adapters.system.plugin.package import PluginInstallVersionTarget, PluginPackageManager
from app.runtime.compat.readiness import plugin_multi_version_blockers
from app.runtime.extensions.plugin.version import (
    PLUGIN_FALLBACK_VERSION,
    ensure_plugin_version_dir_available,
    migrate_legacy_plugin_layout,
    plugin_version_dirs,
    read_declared_plugin_version,
    register_plugin_version,
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


def _resolve_plugin_install_target(
    plugin_id: str,
    plugin_dir: Path,
    staged_source_dir: Path,
) -> Optional[PluginInstallVersionTarget]:
    """决定已就位的暂存源码应当落盘到插件根目录下的哪个版本子目录。

    调用方需确保并存检查已经通过——本函数只做机械的目标目录决策，不重复扫描
    写法。声明版本号缺失时沿用平铺布局，不为无版本号的插件强行造版本目录；
    已装内容是平铺布局且声明版本号与待装版本相同时同样留在平铺布局，视为一次
    原地重装，不为同版本重装凭空造出版本目录。其余情况下需要一个版本目录来
    承载待装内容：仍是平铺布局时先把存量源码原地迁移腾出插件根目录，迁移失败
    时拒绝安装以保住存量源码的可加载性；已经是版本化布局时直接申请目录名。
    这个组合只能落在组合根——版本目录布局和存量迁移都属于运行时扩展包，不允许
    被适配器层引用。

    :param plugin_id: 插件ID
    :param plugin_dir: 插件根目录；可能尚不存在
    :param staged_source_dir: 已就位的待装源码目录
    :return: 版本目录名与版本号；沿用平铺布局时为 None
    :raise ValueError: 版本号不是合法目录名，或与已装版本大小写撞名
    :raise RuntimeError: 存量平铺布局迁移到版本目录失败
    """
    incoming_version = read_declared_plugin_version(staged_source_dir / "__init__.py")
    if not incoming_version:
        return None

    flat_init = plugin_dir / "__init__.py"
    if not plugin_version_dirs(plugin_dir) and flat_init.is_file():
        installed_version = read_declared_plugin_version(flat_init) or PLUGIN_FALLBACK_VERSION
        if installed_version == incoming_version:
            return None
        migrated = migrate_legacy_plugin_layout(plugin_dir)
        if migrated is None or migrated.parent != plugin_dir:
            raise RuntimeError(
                f"插件 {plugin_id} 存量源码迁移到版本目录失败，安装已取消"
            )

    dir_name = ensure_plugin_version_dir_available(plugin_dir, incoming_version)
    return PluginInstallVersionTarget(subdirectory=dir_name, version=incoming_version)


def _register_plugin_install_version(plugin_dir: Path, version: str, source: str) -> None:
    """把已落盘的版本目录登记进版本元信息并置为当前版本。

    :param plugin_dir: 插件根目录
    :param version: 已落盘的版本号
    :param source: 版本来源标签，如 market、local
    """
    register_plugin_version(plugin_dir, version, source)


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
            install_target_resolver=_resolve_plugin_install_target,
            install_version_registrar=_register_plugin_install_version,
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
