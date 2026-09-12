"""插件市场技术依赖的唯一启动组合 owner。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

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
    remove_plugin_installed_version,
    resolve_instance_version_dir,
    resolve_plugin_version_dir,
)
from app.runtime.settings import get_runtime_setting
from app.schemas.plugin import PluginInstance


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
    :param plugin_dir: 插件根目录；未声明源码（尚未安装）时不检查
    :param source_dir: 待安装的插件源码目录
    :return: 拒绝说明；无需拒绝时为 None
    """
    # 已装源码要按当前布局解析：插件迁到版本目录布局后根目录不再有 __init__.py，
    # 直接判根目录会让这道守卫从此每次都放行，正好从第二个版本开始永久失效。
    installed_dir = resolve_plugin_version_dir(plugin_dir)
    installed_init = installed_dir / "__init__.py"
    if not installed_init.is_file():
        return None
    installed_version = read_declared_plugin_version(installed_init) or PLUGIN_FALLBACK_VERSION
    incoming_version = (
        read_declared_plugin_version(source_dir / "__init__.py") or PLUGIN_FALLBACK_VERSION
    )
    if installed_version == incoming_version:
        return None
    try:
        expected_versions = _plugin_expected_versions_after_switch(
            plugin_id,
            plugin_dir,
            installed_version,
            incoming_version,
        )
    except RuntimeError as error:
        return str(error)
    if len(expected_versions) <= 1:
        return None
    on_disk = plugin_version_dirs(plugin_dir)
    source_dirs = [
        on_disk[version]
        for version in sorted(expected_versions)
        if version in on_disk
    ]
    if (
        installed_version in expected_versions
        and installed_dir.is_dir()
        and installed_dir not in source_dirs
    ):
        source_dirs.insert(0, installed_dir)
    source_dirs.append(source_dir)
    blockers = plugin_multi_version_blockers(plugin_id.lower(), source_dirs)
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


def _register_plugin_install_version(
    plugin_dir: Path, version: str, source: str
) -> Optional[str]:
    """把已落盘的版本目录登记进版本元信息并置为当前版本，返回登记前的当前版本号。

    返回值供安装失败清理据此精确复原当前版本，不必在回滚时靠猜。

    :param plugin_dir: 插件根目录
    :param version: 已落盘的版本号
    :param source: 版本来源标签，如 market、local
    :return: 登记前元信息里的当前版本号；插件在本次登记前没有任何已装版本时为 None
    """
    _, previous_current = register_plugin_version(plugin_dir, version, source)
    return previous_current


def _rollback_plugin_install_version(
    plugin_dir: Path, version: str, previous_current: Optional[str]
) -> None:
    """安装失败时回滚单个版本目录及其版本元信息登记，不牵连插件的其它已装版本。

    版本目录布局和版本元信息读写都属于运行时扩展包，不允许被适配器层引用，
    因此只能落在组合根。

    :param plugin_dir: 插件根目录
    :param version: 安装失败需要回滚的版本号
    :param previous_current: 登记本次失败版本之前元信息里的当前版本号，由
        ``_register_plugin_install_version`` 返回并逐层穿透而来；插件在
        本次登记前没有任何已装版本时为 None
    """
    remove_plugin_installed_version(plugin_dir, version, previous_current)


@dataclass(frozen=True, slots=True)
class PluginMarketComposition:
    """保存插件市场相关 Transport、Client、Package 和 Dependency owner。"""

    transport: PluginMarketTransport
    client: PluginMarketClient
    package: PluginPackageManager
    health: PluginRuntimeHealth
    dependency: PluginDependencyInstaller


_market_client: Optional[PluginMarketClient] = None


def _plugin_expected_versions_after_switch(
    plugin_id: str,
    plugin_root: Path,
    installed_version: str,
    incoming_version: str,
) -> set[str]:
    """计算安装完成后本体与分身实际期望加载的版本集合。

    目录中存在旧版本只代表它尚未回收，不能据此阻止普通本体升级；只有实际的
    host/clone 绑定在安装后仍要求旧版本，且至少有一个实例跟随新当前版本时，
    新旧载荷才会在同一运行时中并存。所有实例都钉在旧版本时，安装新载荷只改变
    磁盘上的 current，不会制造正在运行的并存，静态写法体检不应拦截这种升级。
    安装服务在 Runtime 物化后调用本守卫，因此这里通过 existing-manager 读取持久
    化绑定，不触发隐式 Runtime 构造。尚未物化时只有默认 host 跟随新当前版本；已
    物化但绑定查询失败则拒绝，避免把未知状态当成安全状态。
    """
    on_disk = plugin_version_dirs(plugin_root)
    available_versions = set(on_disk) | {installed_version}
    expected_versions: set[str] = set()
    try:
        from app.application.plugin.runtime import get_existing_plugin_manager

        manager = get_existing_plugin_manager()
    except (ImportError, RuntimeError):
        manager = None
    if manager is None:
        return {incoming_version}

    try:
        bindings: list[PluginInstance] = []
        get_host = getattr(manager, "get_plugin_version_binding", None)
        host = get_host(plugin_id) if callable(get_host) else None
        if host is None or host.pinned_version is None:
            expected_versions.add(incoming_version)
        else:
            host_version = host.pinned_version or installed_version
            if host_version in available_versions:
                expected_versions.add(host_version)
            else:
                expected_versions.add(incoming_version)
        get_instances = getattr(manager, "get_plugin_source_instances", None)
        if callable(get_instances):
            bindings.extend(get_instances(plugin_id) or [])
        else:
            raise RuntimeError("插件运行时没有提供分身版本绑定查询")
        for binding in bindings:
            if binding.pinned_version is None:
                expected_versions.add(incoming_version)
                continue
            expected = binding.pinned_version or installed_version
            # resolve_instance_version_dir 在绑定目录已经被回收时会安全回落到
            # 当前版本；把这一事实带入并存集合，避免用一个已不存在的旧版本
            # 把升级错误地判成真实并存。
            if expected in available_versions:
                expected_versions.add(expected)
            else:
                expected_versions.add(incoming_version)
        return expected_versions
    except Exception as error:  # noqa: BLE001 - 并存判据未知时必须拒绝
        raise RuntimeError(
            f"无法确认插件 {plugin_id} 的版本绑定，拒绝不安全的版本切换：{error}"
        ) from error


def _bound_plugin_directories(plugin_id: str) -> list[Path]:
    """解析启动时本体与分身实际会加载的插件版本目录。

    依赖安装发生在市场 owner 构造之后、插件 Runtime 物化之后；这里使用无副作用
    的 existing-manager 探测，避免为依赖扫描隐式创建运行时。未物化时回落到磁盘
    上全部版本目录，保证早期兼容入口仍不会漏扫依赖。
    """
    plugin_root = Path(get_runtime_setting("ROOT_PATH")) / "app" / "plugins" / plugin_id.lower()
    try:
        from app.application.plugin.runtime import get_existing_plugin_manager

        manager = get_existing_plugin_manager()
    except (ImportError, RuntimeError):
        manager = None
    if manager is None:
        return list(plugin_version_dirs(plugin_root).values()) or [plugin_root]

    bindings = []
    get_binding = getattr(manager, "get_plugin_version_binding", None)
    host_binding = None
    if callable(get_binding):
        host_binding = get_binding(plugin_id)
        if host_binding is not None:
            bindings.append(host_binding)
    get_instances = getattr(manager, "get_plugin_source_instances", None)
    if callable(get_instances):
        bindings.extend(get_instances(plugin_id) or [])

    # 有显式 host 绑定时，None 不是一个额外实例；解析它会把当前版本错误地
    # 纳入依赖扫描，导致「host 旧版就绪、当前版缺依赖」被合并成错误结论。
    directories = [
        resolve_instance_version_dir(plugin_root, host_binding)
    ] if host_binding is not None else [resolve_instance_version_dir(plugin_root, None)]
    directories.extend(
        resolve_instance_version_dir(plugin_root, binding)
        for binding in bindings
    )
    result: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        resolved = directory.resolve()
        if resolved in seen or not directory.is_dir():
            continue
        seen.add(resolved)
        result.append(directory)
    return result


def compose_plugin_market(
    *,
    installed_plugins_provider: Callable[[], list[str]],
    plugin_directories_provider: Optional[Callable[[str], Iterable[Path]]] = None,
) -> PluginMarketComposition:
    """一次性构造插件市场技术依赖，供同一 lifespan 内所有用例复用。

    ``plugin_directories_provider`` 由启动层按实例绑定提供实际生效的版本源码目录；
    缺省时 installer 会自行发现版本目录，兼容尚未建立 Runtime 的旧调用方。
    """
    global _market_client
    root_path = Path(get_runtime_setting("ROOT_PATH"))
    plugin_root = root_path / "app" / "plugins"
    transport = PluginMarketTransport.get_existing_instance() or PluginMarketTransport()
    client = PluginMarketClient(transport)
    health = PluginRuntimeHealth()
    directories_provider = plugin_directories_provider or _bound_plugin_directories
    composition = PluginMarketComposition(
        transport=transport,
        client=client,
        package=PluginPackageManager(
            source=PluginPackageSourceClient(transport),
            plugin_root=plugin_root,
            version_switch_guard=_reject_incompatible_plugin_version_switch,
            install_target_resolver=_resolve_plugin_install_target,
            install_version_registrar=_register_plugin_install_version,
            install_version_rollback=_rollback_plugin_install_version,
        ),
        dependency=PluginDependencyInstaller(
            health,
            installed_plugins_provider=installed_plugins_provider,
            plugin_dir=plugin_root,
            plugin_directories_provider=directories_provider,
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
