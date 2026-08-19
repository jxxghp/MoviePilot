import ast
import asyncio
import importlib.util
import inspect
import os
import posixpath
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union, Callable, Tuple

from fastapi import HTTPException
from starlette import status
from watchfiles import watch

from app.schemas.plugin import Plugin as _SchemaPlugin
from app.schemas.plugin import PluginDashboard as _SchemaPluginDashboard
from app.foundation.crypto import RSAUtils
from app.foundation.singleton import Singleton
from app.foundation.version import compare_version
from app.runtime.log import logger
from app.runtime.config import settings
from app.runtime.events import EventHandlerBinding, eventmanager
from app.runtime.reload import ConfigReloadMixin
from app.runtime.extensions.contract import supports_extension_hook
from app.runtime.extensions.instance import DEFAULT_INSTANCE_ID
from app.runtime.extensions.plugin.projection import PluginExtension, PluginProjection
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.runtime.extensions.plugin.storage import get_plugin_storage
from app.runtime.extensions.plugin.system import get_plugin_system
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import EventType, SystemConfigKey

LegacyDiagnosticsConfigurator = Callable[..., None]
LegacyImportScanner = Callable[..., None]
LegacyPluginImportPreparer = Callable[..., None]
PluginInstallReporter = Callable[..., None]
SiteAuthLevelProvider = Callable[[], int]
PluginCatalogFactory = Callable[["PluginManager"], Any]
PluginDatabaseEnsurer = Callable[[str, str], None]
PluginDatabaseReleaser = Callable[[str], None]
PluginDatabaseDestroyer = Callable[[str, str], None]


def _ignore_legacy_diagnostics(**_kwargs) -> None:
    """在启动组合根尚未注入兼容服务时保持插件加载可用。"""


def _ignore_plugin_resource_imports(**_kwargs) -> None:
    """未进入应用启动组合时不主动创建进程级宿主资源。"""


def _unavailable_site_auth_level() -> int:
    """站点能力尚未装配时返回未认证等级。"""
    return 0


def _unavailable_plugin_catalog_factory(_manager: "PluginManager") -> Any:
    """在启动组合根尚未装配目录用例时拒绝隐式跨层构造。"""
    raise RuntimeError("插件目录应用服务尚未由启动组合根装配")


def _ignore_plugin_database_ensure(_plugin_id: str, _instance_id: str) -> None:
    """插件数据库框架尚未装配时跳过建库，避免扩展层反向依赖 DB 层。"""


def _ignore_plugin_database_release(_plugin_id: str) -> None:
    """插件数据库框架尚未装配时跳过连接释放。"""


def _ignore_plugin_database_destroy(_plugin_id: str, _instance_id: str) -> None:
    """插件数据库框架尚未装配时跳过库文件销毁。"""


_legacy_diagnostics_configurator: LegacyDiagnosticsConfigurator = (
    _ignore_legacy_diagnostics
)
_legacy_import_scanner: LegacyImportScanner = _ignore_legacy_diagnostics
_legacy_plugin_import_preparer: LegacyPluginImportPreparer = (
    _ignore_plugin_resource_imports
)
_plugin_install_reporter: PluginInstallReporter = _ignore_legacy_diagnostics
_site_auth_level_provider: SiteAuthLevelProvider = _unavailable_site_auth_level
_plugin_catalog_factory: PluginCatalogFactory = _unavailable_plugin_catalog_factory
_plugin_database_ensure: PluginDatabaseEnsurer = _ignore_plugin_database_ensure
_plugin_database_release: PluginDatabaseReleaser = _ignore_plugin_database_release
_plugin_database_destroy: PluginDatabaseDestroyer = _ignore_plugin_database_destroy


def configure_plugin_legacy_import_services(
    *,
    diagnostics_configurator: LegacyDiagnosticsConfigurator,
    import_scanner: LegacyImportScanner,
) -> None:
    """由启动组合根注入插件旧导入诊断服务，避免扩展层反向依赖兼容层。"""
    global _legacy_diagnostics_configurator, _legacy_import_scanner
    _legacy_diagnostics_configurator = diagnostics_configurator
    _legacy_import_scanner = import_scanner


def configure_plugin_resource_import_preparer(
    preparer: LegacyPluginImportPreparer,
) -> None:
    """注入旧插件导入前的宿主资源准备器。"""
    global _legacy_plugin_import_preparer
    _legacy_plugin_import_preparer = preparer


def configure_plugin_install_reporter(reporter: PluginInstallReporter) -> None:
    """由启动组合根注入插件安装上报器，避免扩展层依赖远程服务。"""
    global _plugin_install_reporter
    _plugin_install_reporter = reporter


def configure_site_auth_level_provider(provider: SiteAuthLevelProvider) -> None:
    """由启动组合根注入站点认证等级，避免扩展运行时依赖应用服务。"""
    global _site_auth_level_provider
    _site_auth_level_provider = provider


def configure_plugin_catalog_factory(factory: PluginCatalogFactory) -> None:
    """由启动组合根注入插件目录应用服务工厂，消除 Runtime 反向依赖。"""
    global _plugin_catalog_factory
    _plugin_catalog_factory = factory


def _configure_plugin_database_lifecycle(
    *,
    ensure: PluginDatabaseEnsurer,
    release: PluginDatabaseReleaser,
    destroy: PluginDatabaseDestroyer,
) -> None:
    """
    由插件数据库框架（app.db.plugin）自注册建库、释放与销毁钩子。

    runtime/extensions 层不得反向依赖 db 层，因此这里只声明可注入的钩子；
    实现由 app.db.plugin 包首次被 import 时自行注入。
    :param ensure: 按插件实例声明建库，未声明模型也未声明迁移目录时不做任何事
    :param release: 释放某插件全部实例的数据库连接，不销毁库文件
    :param destroy: 销毁某插件实例的数据库，含库文件本身，不可逆
    """
    global _plugin_database_ensure, _plugin_database_release, _plugin_database_destroy
    _plugin_database_ensure = ensure
    _plugin_database_release = release
    _plugin_database_destroy = destroy


class PluginManager(ConfigReloadMixin, metaclass=Singleton):
    """插件管理器"""
    CONFIG_WATCH = {"DEV", "PLUGIN_AUTO_RELOAD", "PLUGIN_LOCAL_REPO_PATHS"}
    AGENT_TOOLS_BUILD_MAX_ATTEMPTS = 3

    def __init__(self):
        """初始化插件注册表、缓存和开发模式监控状态。"""
        self._plugin_registry = PluginRegistry()
        # 旧属性继续引用注册表拥有的可变字典，保持插件和测试的访问身份。
        self._plugins = self._plugin_registry.classes
        self._running_plugins = self._plugin_registry.running
        # 配置Key
        self._config_key: str = "plugin.%s"
        # 监控线程
        self._monitor_thread: Optional[threading.Thread] = None
        # 监控停止事件
        self._stop_monitor_event = threading.Event()
        # 本地插件同步写入运行目录后的短时忽略窗口
        self._recent_local_sync: Dict[str, float] = {}
        # 插件智能体工具注册表缓存，插件启停或配置生效时主动失效。
        self._plugin_agent_tools_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._plugin_agent_tools_cache_lock = threading.Lock()
        self._plugin_agent_tools_revision: int = 0
        # 事件总线只通过通用解析器访问运行中的插件实例。
        eventmanager.register_handler_instance_resolver(
            "plugins",
            self.resolve_event_handler_instance,
        )
        # 开发者模式监测插件修改
        if settings.DEV or settings.PLUGIN_AUTO_RELOAD:
            self.__start_monitor()

    def resolve_event_handler_instance(
            self,
            owner_class: Type[Any],
    ) -> Optional[List[EventHandlerBinding]]:
        """为插件声明的事件方法解析当前运行实例绑定列表。"""
        plugin_id = owner_class.__name__
        # 旧测试与部分扩展会替换私有映射来构造隔离运行态，解析器继续尊重该接缝。
        if plugin_id not in self._plugins:
            return None
        plugin = self._running_plugins.get(plugin_id)
        owner_name = plugin_id
        if plugin and callable(getattr(plugin, "get_name", None)):
            owner_name = plugin.get_name()
        return [
            EventHandlerBinding(
                instance=plugin,
                owner_name=owner_name,
                run_sync_in_threadpool=True,
            )
        ]

    def init_config(self):
        """按最新系统配置完整重启插件。"""
        # 停止已有插件
        self.stop()
        # 启动插件
        self.start()

    def start(self, pid: Optional[str] = None):
        """
        启动加载插件
        :param pid: 插件ID，为空加载所有插件
        """

        _legacy_diagnostics_configurator(
            enabled=settings.DEBUG,
            emitter=logger.warning,
        )

        def check_module(module: Any):
            """
            检查模块
            """
            if not hasattr(module, 'init_plugin') or not hasattr(module, "plugin_name"):
                return False
            return True

        # 已安装插件
        installed_plugins = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        # 扫描插件目录，只加载符合条件的插件
        plugins = self._load_selective_plugins(pid, installed_plugins, check_module)
        # 排序
        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        for plugin in plugins:
            plugin_id = plugin.__name__
            if pid and plugin_id != pid:
                continue
            try:
                # 判断插件是否满足认证要求，如不满足则不进行实例化
                if not self.__set_and_check_auth_level(plugin=plugin):
                    # 如果是插件热更新实例，这里则进行替换
                    if plugin_id in self._plugins:
                        self._plugins[plugin_id] = plugin
                    continue
                # 存储Class
                self._plugins[plugin_id] = plugin
                # 生成实例
                plugin_obj = plugin()
                extension = PluginExtension(plugin_obj, plugin_id)
                # 生效插件配置
                extension.initialize(self.get_plugin_config(plugin_id))
                # 按插件声明建立其数据库，未声明模型或迁移目录时不做任何事
                _plugin_database_ensure(plugin_id, DEFAULT_INSTANCE_ID)
                # 存储运行实例
                self._running_plugins[plugin_id] = plugin_obj
                logger.info(f"加载插件：{plugin_id} 版本：{plugin_obj.plugin_version}")
                # 同步插件声明的渠道能力
                self._sync_channel_capabilities(plugin_id)
                # 启用的插件才设置事件注册状态可用
                if extension.is_enabled():
                    eventmanager.enable_event_handler(plugin)
                else:
                    eventmanager.disable_event_handler(plugin)
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{str(err)} - {traceback.format_exc()}")
        self.clear_plugin_agent_tools_cache()

    def init_plugin(self, plugin_id: str, conf: dict):
        """
        初始化插件
        :param plugin_id: 插件ID
        :param conf: 插件配置
        """
        plugin = self._running_plugins.get(plugin_id)
        if not plugin:
            return
        extension = PluginExtension(plugin, plugin_id)
        # 初始化插件
        extension.initialize(conf)
        # 检查插件状态并启用/禁用事件处理器
        if extension.is_enabled():
            # 启用插件类的事件处理器
            eventmanager.enable_event_handler(type(plugin))
        else:
            # 禁用插件类的事件处理器
            eventmanager.disable_event_handler(type(plugin))
        # 配置变更可能启用或停用插件，重新同步渠道能力登记
        self._sync_channel_capabilities(plugin_id)
        self.clear_plugin_agent_tools_cache()

    def clear_plugin_agent_tools_cache(self) -> None:
        """
        清空插件智能体工具注册表缓存。
        """
        with self._plugin_agent_tools_cache_lock:
            self._plugin_agent_tools_cache.clear()
            self._plugin_agent_tools_revision += 1

    def get_plugin_agent_tools_revision(self) -> int:
        """
        获取插件智能体工具注册表版本号。
        """
        with self._plugin_agent_tools_cache_lock:
            return self._plugin_agent_tools_revision

    def stop(self, pid: Optional[str] = None):
        """
        停止插件服务
        :param pid: 插件ID，为空停止所有插件
        """
        # 停止插件
        if pid:
            logger.info(f"正在停止插件 {pid}...")
            plugin_obj = self._running_plugins.get(pid)
            if not plugin_obj:
                # 指定插件可能在上次加载时已导入模块但初始化失败，此时不会进入运行态列表。
                # 仍需继续清理类缓存和 sys.modules，避免后续热重载反复复用旧模块。
                logger.debug(f"插件 {pid} 不存在或未加载")
                plugins = {}
            else:
                plugins = {pid: plugin_obj}
        else:
            logger.info("正在停止所有插件...")
            plugins = self._running_plugins
        for plugin_id, plugin in plugins.items():
            eventmanager.disable_event_handler(type(plugin))
            self.__stop_plugin(plugin)
            # 插件停止后撤销其渠道能力登记，不留残留
            self._revoke_channel_capabilities(plugin_id)
            # 停止只释放数据库连接，不销毁库文件——销毁只在明确删除插件数据的路径触发
            _plugin_database_release(plugin_id)
        # 清空对象
        if pid:
            # 清空指定插件
            self._plugin_registry.remove(pid)
            # 清除插件模块缓存，包括所有子模块
            self._clear_plugin_modules(pid)
        else:
            # 清空
            self._plugin_registry.clear()
            # 清除所有插件模块缓存
            self._clear_plugin_modules()
        self.clear_plugin_agent_tools_cache()
        logger.info("插件停止完成")

    @staticmethod
    def _load_selective_plugins(pid: Optional[str], installed_plugins: List[str],
                                check_module_func: Callable) -> List[Any]:
        """
        选择性加载插件，只import符合条件的插件
        :param pid: 指定插件ID，为空则加载所有已安装插件
        :param installed_plugins: 已安装插件列表
        :param check_module_func: 模块检查函数
        :return: 插件类列表
        """
        import importlib

        plugins = []
        plugins_dir = settings.ROOT_PATH / "app" / "plugins"

        if not plugins_dir.exists():
            logger.warning(f"插件目录不存在：{plugins_dir}")
            return plugins

        # 确定需要加载的插件目录名称列表
        if pid:
            # 加载指定插件
            target_plugins = [pid.lower()]
        else:
            # 加载已安装插件
            target_plugins = [plugin_id.lower() for plugin_id in installed_plugins]

        if not target_plugins:
            logger.debug("没有需要加载的插件")
            return plugins

        # 扫描plugins目录
        _loaded_modules = set()
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir() or plugin_dir.name.startswith('_'):
                continue

            # 检查是否是需要加载的插件
            if plugin_dir.name not in target_plugins:
                logger.debug(f"跳过插件目录：{plugin_dir.name}（不在加载列表中）")
                continue

            # 检查__init__.py是否存在
            init_file = plugin_dir / "__init__.py"
            if not init_file.exists():
                logger.debug(f"跳过插件目录：{plugin_dir.name}（缺少__init__.py）")
                continue

            try:
                # 构建模块名
                module_name = f"app.plugins.{plugin_dir.name}"
                logger.debug(f"正在导入插件模块：{module_name}")

                # 旧插件可能直接导入带宿主资源前置条件的第三方包。资源必须在
                # Python 执行插件模块顶层代码前就绪，否则导入副作用无法安全回滚。
                _legacy_plugin_import_preparer(
                    plugin_id=plugin_dir.name,
                    plugin_dir=plugin_dir,
                )

                _legacy_import_scanner(
                    plugin_id=plugin_dir.name,
                    plugin_dir=plugin_dir,
                )

                # 导入模块
                module = importlib.import_module(module_name)

                # 检查模块中的类
                for name, obj in module.__dict__.items():
                    if name.startswith('_') or not isinstance(obj, type):
                        continue
                    if name in _loaded_modules:
                        continue
                    if check_module_func(obj):
                        _loaded_modules.add(name)
                        plugins.append(obj)
                        logger.debug(f"找到符合条件的插件类：{name}")
                        break

            except Exception as err:
                logger.error(f"加载插件 {plugin_dir.name} 失败：{str(err)} - {traceback.format_exc()}")

        return plugins

    @property
    def running_plugins(self) -> Dict[str, Any]:
        """
        获取运行态插件列表
        :return: 运行态插件列表
        """
        return self._plugin_registry.running

    @property
    def plugins(self) -> Dict[str, Any]:
        """
        获取插件列表
        :return: 插件列表
        """
        return self._plugin_registry.classes

    def on_config_changed(self):
        """在插件监控配置变化后重建文件监控。"""
        self.reload_monitor()

    def get_reload_name(self) -> str:
        """返回配置重载日志使用的功能名称。"""
        return "插件文件修改监测"

    def reload_monitor(self):
        """
        重新加载插件文件修改监测
        """
        if settings.DEV or settings.PLUGIN_AUTO_RELOAD:
            # 先关闭已有监测，再重新启动
            self.stop_monitor()
            self.__start_monitor()
        else:
            self.stop_monitor()

    def __start_monitor(self):
        """
        启用监测插件文件修改监测
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.info("插件文件修改监测已经在运行中...")
            return

        logger.info("开始监测插件文件修改...")

        # 在启动新线程之前，确保停止事件是清除状态
        self._stop_monitor_event.clear()

        # 创建并启动监控线程
        self._monitor_thread = threading.Thread(
            target=self._run_file_watcher,
            daemon=True
        )
        self._monitor_thread.start()

    def stop_monitor(self):
        """
        停止监测插件文件修改监测
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.info("正在停止插件文件修改监测...")
            self._stop_monitor_event.set()
            self._monitor_thread.join(timeout=5)
            if self._monitor_thread.is_alive():
                logger.warning("插件文件修改监测线程在5秒内未能正常停止。")
            self._monitor_thread = None
            logger.info("插件文件修改监测停止完成")
        else:
            logger.info("未启用插件文件修改监测，无需停止")

    def _run_file_watcher(self):
        """
        运行 watchfiles 监视器的主循环。
        """
        # 监视插件目录
        plugin_paths = [str(settings.ROOT_PATH / "app" / "plugins")]
        for local_repo_path in get_plugin_system().local_repo_paths():
            if local_repo_path.exists() and local_repo_path.is_dir():
                plugin_paths.append(str(local_repo_path))
        logger.info(">>> 监控线程已启动，准备进入watch循环...")
        # 使用 watchfiles 监视目录变化，并响应变化事件
        # Todo: yield_on_timeout = True 时，每秒检查停止事件，会返回空集合；后续可以考虑用来做心跳之类的功能？
        for changes in watch(*plugin_paths, stop_event=self._stop_monitor_event, rust_timeout=1000,
                             yield_on_timeout=True):
            # 如果收到停止事件，退出循环
            if not changes:
                continue

            # 处理变化事件
            plugins_to_reload = set()
            local_plugins_to_sync = {}
            for _change_type, path_str in changes:
                event_path = Path(path_str)

                # 跳过 pycache 目录中的文件
                if "__pycache__" in event_path.parts:
                    continue

                if event_path.name == "requirements.txt":
                    candidate = self._get_local_plugin_candidate_from_path(event_path)
                    if candidate:
                        if candidate.get("compatible") is False:
                            logger.info(
                                f"检测到本地插件 {candidate.get('id')} 依赖文件变化，"
                                f"但跳过处理：{candidate.get('skip_reason')}"
                            )
                            continue
                        logger.warn(f"检测到本地插件 {candidate.get('id')} 依赖文件变化，请重新安装本地插件以安装依赖")
                    continue

                federated_change = self._get_federated_plugin_change(event_path)
                if federated_change:
                    pid, candidate, remote_entry_ready = federated_change
                    # 运行目录由构建方直接写入；外部本地仓库只在入口完整时同步运行副本。
                    if candidate and remote_entry_ready:
                        if candidate.get("compatible") is False:
                            logger.info(
                                f"检测到本地插件 {pid} 联邦构建产物变化，"
                                f"但跳过同步：{candidate.get('skip_reason')}"
                            )
                        elif pid not in local_plugins_to_sync:
                            local_plugins_to_sync[pid] = (candidate, event_path, False)
                    continue

                # 跳过非 .py 文件
                if not event_path.name.endswith(".py"):
                    continue

                # 解析插件ID
                runtime_pid = self._get_plugin_id_from_path(event_path)
                local_candidate = self._get_local_plugin_candidate_from_path(event_path) if not runtime_pid else None
                if runtime_pid:
                    last_sync_time = self._recent_local_sync.get(runtime_pid)
                    if last_sync_time and time.time() - last_sync_time < 2:
                        continue
                    # 运行目录变化只重载，不能反向触发本地同步。
                    plugins_to_reload.add(runtime_pid)
                elif local_candidate:
                    if local_candidate.get("compatible") is False:
                        package_version = local_candidate.get("package_version")
                        source_root = f"plugins.{package_version}" if package_version else "plugins"
                        logger.info(
                            f"检测到本地插件 {local_candidate.get('id')} 文件变化，来源：{source_root}，"
                            f"文件：{event_path}，但跳过同步：{local_candidate.get('skip_reason')}"
                        )
                        continue
                    local_plugins_to_sync[local_candidate.get("id")] = (local_candidate, event_path, True)

            for pid, (candidate, event_path, should_reload) in local_plugins_to_sync.items():
                package_version = candidate.get("package_version")
                source_root = f"plugins.{package_version}" if package_version else "plugins"
                change_name = "Python 文件" if should_reload else "联邦构建产物"
                logger.info(f"检测到本地插件 {pid} {change_name}变化，来源：{source_root}，文件：{event_path}")
                if self._sync_local_plugin_if_installed(pid, candidate) and should_reload:
                    plugins_to_reload.add(pid)

            # 触发重载
            if plugins_to_reload:
                logger.info(f"检测到插件文件变化，准备重载: {list(plugins_to_reload)}")
                for pid in plugins_to_reload:
                    try:
                        self.reload_plugin(pid)
                    except Exception as e:
                        logger.error(f"插件 {pid} 热重载失败: {e}", exc_info=True)

    def _get_federated_plugin_change(
        self,
        event_path: Path,
    ) -> Optional[Tuple[str, Optional[dict], bool]]:
        """
        识别运行态 Vue 插件声明目录内的构建产物变化。

        :return: 插件 ID、本地仓库候选和联邦入口是否完整；非联邦目录变化返回 None。
        """
        try:
            event_path = event_path.resolve()
            candidate = self._get_local_plugin_candidate_from_path(event_path)
            if candidate:
                pid = candidate.get("id")
                plugin_dir = Path(candidate.get("path")).resolve()
            else:
                runtime_root = (settings.ROOT_PATH / "app" / "plugins").resolve()
                if not event_path.is_relative_to(runtime_root):
                    return None
                relative_parts = event_path.relative_to(runtime_root).parts
                if not relative_parts:
                    return None
                plugin_dir = runtime_root / relative_parts[0]
                pid = next(
                    (
                        plugin_id
                        for plugin_id in self._running_plugins
                        if plugin_id.lower() == relative_parts[0].lower()
                    ),
                    None,
                )

            if not pid:
                return None
            plugin = self._running_plugins.get(pid)
            if not plugin:
                return None

            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue" or not isinstance(dist_path, str) or not dist_path:
                return None

            relative_dist_path = Path(dist_path)
            if relative_dist_path.is_absolute() or ".." in relative_dist_path.parts or "\\" in dist_path:
                return None

            plugin_dir = plugin_dir.resolve()
            dist_dir = (plugin_dir / relative_dist_path).resolve()
            if (
                dist_dir == plugin_dir
                or not dist_dir.is_relative_to(plugin_dir)
                or not event_path.is_relative_to(dist_dir)
            ):
                return None

            remote_entry = dist_dir / "remoteEntry.js"
            remote_entry_ready = (
                remote_entry.is_file()
                and remote_entry.resolve().is_relative_to(plugin_dir)
            )
            return pid, candidate, remote_entry_ready
        except Exception as e:
            logger.error(f"识别插件联邦构建产物变化时出错: {e}")
            return None

    @staticmethod
    def _get_plugin_id_from_path(event_path: Path) -> Optional[str]:
        """
        根据文件路径解析出插件的ID。
        :param event_path: 被修改文件的 Path 对象。
        :return: 插件ID字符串，如果不是有效插件文件则返回 None。
        """
        try:
            event_path = event_path.resolve()
            plugins_root = settings.ROOT_PATH / "app" / "plugins"
            # 确保修改的文件在 plugins 目录下
            if not event_path.is_relative_to(plugins_root):
                return None

            try:
                plugin_dir_name = event_path.relative_to(plugins_root).parts[0]
                plugin_dir = plugins_root / plugin_dir_name
            except (ValueError, IndexError):
                return None

            init_file = plugin_dir / "__init__.py"
            if not init_file.exists():
                return None

            # 读取 __init__.py 文件，查找插件主类名
            with open(init_file, "r", encoding="utf-8", errors="replace") as f:
                source_code = f.read()

            tree = ast.parse(source_code)

            # 遍历AST，查找继承自 _PluginBase 的类
            for node in ast.walk(tree):
                # 检查节点是否为类定义
                if isinstance(node, ast.ClassDef):
                    # 遍历该类的所有基类
                    for base in node.bases:
                        # 检查基类是否是我们寻找的 _PluginBase
                        # ast.Name 用于处理简单的基类名
                        if isinstance(base, ast.Name) and base.id == '_PluginBase':
                            # 返回这个类的名字
                            return node.name

            return None
        except Exception as e:
            logger.error(f"从路径解析插件ID时出错: {e}")
            return None

    @staticmethod
    def _get_local_plugin_candidate_from_path(event_path: Path) -> Optional[dict]:
        """
        根据本地插件仓库路径解析具体插件候选，保留 plugins/plugins.v2 来源差异
        """
        try:
            event_path = event_path.resolve()
            for local_repo_path in get_plugin_system().local_repo_paths():
                if not local_repo_path.exists() or not local_repo_path.is_dir():
                    continue
                if not event_path.is_relative_to(local_repo_path):
                    continue
                try:
                    relative_parts = event_path.relative_to(local_repo_path).parts
                except (ValueError, IndexError):
                    continue
                if len(relative_parts) < 2:
                    continue
                if relative_parts[0] == "plugins":
                    package_version = ""
                elif relative_parts[0].startswith("plugins."):
                    package_version = relative_parts[0].split(".", 1)[1]
                else:
                    continue
                plugin_dir_name = relative_parts[1]
                candidate = get_plugin_system().local_candidate(
                    plugin_dir_name,
                    package_version=package_version,
                    repo_path=local_repo_path,
                    strict_compat=False,
                    strict_system_version=not settings.DEV,
                )
                if candidate:
                    return candidate
            return None
        except Exception as e:
            logger.error(f"从本地插件仓库路径解析插件候选时出错: {e}")
            return None

    @staticmethod
    def _sync_local_plugin_if_installed(pid: str, candidate: Optional[dict] = None) -> bool:
        """
        已安装本地插件源码变化时，同步到运行目录
        """
        installed_plugins = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        if pid not in installed_plugins:
            logger.info(f"本地插件 {pid} 尚未安装，跳过自动同步和热重载")
            return False

        candidate = candidate or get_plugin_system().local_candidate(pid)
        if not candidate:
            return False
        if candidate.get("compatible") is False:
            logger.info(f"本地插件 {pid} 不满足同步条件，跳过自动同步：{candidate.get('skip_reason')}")
            return False

        source_dir = Path(candidate.get("path"))
        dest_dir = settings.ROOT_PATH / "app" / "plugins" / pid.lower()
        try:
            if not get_plugin_system().package.sync_local(pid, source_dir):
                return False
            PluginManager()._recent_local_sync[pid] = time.time()
            logger.info(f"已同步本地插件 {pid}：{source_dir} -> {dest_dir}")
            return True
        except Exception as e:
            logger.error(f"同步本地插件 {pid} 失败：{e}")
            return False

    @staticmethod
    def __stop_plugin(plugin: Any):
        """
        停止插件
        :param plugin: 插件实例
        """
        extension = PluginExtension(plugin)
        try:
            # 关闭数据库连接与插件后台服务
            extension.terminate()
        except Exception as e:
            logger.warn(f"停止插件 {extension.display_name} 时发生错误: {str(e)}")

    def remove_plugin(self, plugin_id: str):
        """
        从内存中移除一个插件
        :param plugin_id: 插件ID
        """
        self.stop(plugin_id)

    def reload_plugin(self, plugin_id: str):
        """
        将一个插件重新加载到内存
        :param plugin_id: 插件ID
        """
        # 先移除插件实例
        self.stop(plugin_id)
        # 重新加载
        self.start(plugin_id)
        # 广播事件
        eventmanager.send_event(EventType.PluginReload, data={"plugin_id": plugin_id})

    @staticmethod
    def _clear_plugin_modules(plugin_id: Optional[str] = None):
        """
        清除插件及其所有子模块的缓存
        :param plugin_id: 插件ID
        """

        # 构建插件模块前缀
        if plugin_id:
            plugin_module_prefix = f"app.plugins.{plugin_id.lower()}"
        else:
            plugin_module_prefix = "app.plugins"

        # 收集需要删除的模块名（创建模块名列表的副本以避免迭代时修改字典）
        modules_to_remove = []
        for module_name in list(sys.modules.keys()):
            if module_name == plugin_module_prefix or module_name.startswith(plugin_module_prefix + "."):
                modules_to_remove.append(module_name)

        # 删除模块
        for module_name in modules_to_remove:
            try:
                del sys.modules[module_name]
                logger.debug(f"已清除插件模块缓存：{module_name}")
            except KeyError:
                # 模块可能已经被删除
                pass

        importlib.invalidate_caches()
        logger.debug("已清除查找器的缓存")

        if plugin_id:
            if modules_to_remove:
                logger.info(f"插件 {plugin_id} 共清除 {len(modules_to_remove)} 个模块缓存：{modules_to_remove}")
            else:
                logger.debug(f"插件 {plugin_id} 没有找到需要清除的模块缓存")

    def sync(self) -> List[str]:
        """
        安装本地不存在或需要更新的插件
        """

        def install_plugin(plugin):
            start_time = time.time()
            state, msg = get_plugin_system().package.install(
                plugin_id=plugin.id,
                repo_url=plugin.repo_url,
                force_install=True,
            )
            elapsed_time = time.time() - start_time
            if state:
                _plugin_install_reporter(
                    plugin_id=plugin.id,
                    repo_url=plugin.repo_url,
                )
                logger.info(
                    f"插件 {plugin.plugin_name} 安装成功，版本：{plugin.plugin_version}，耗时：{elapsed_time:.2f} 秒")
                sync_plugins.append(plugin.id)
            else:
                logger.error(
                    f"插件 {plugin.plugin_name} v{plugin.plugin_version} 安装失败：{msg}，耗时：{elapsed_time:.2f} 秒")
                failed_plugins.append(plugin.id)

        if get_plugin_system().is_frozen():
            return []

        # 获取已安装插件列表
        install_plugins = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        # 获取远程和本地仓库来源插件列表
        online_plugins = self.get_online_plugins()
        local_repo_plugins = self.get_local_repo_plugins()
        candidate_plugins = self.process_plugins_list(online_plugins + local_repo_plugins, []) \
            if online_plugins or local_repo_plugins else []
        # 确定需要安装的插件
        plugins_to_install = [
            plugin for plugin in candidate_plugins
            if plugin.id in install_plugins
            and plugin.system_version_compatible is not False
            and not self.is_plugin_exists(plugin.id, plugin.plugin_version)
        ]

        if not plugins_to_install:
            return []
        logger.info("开始安装第三方插件...")
        sync_plugins = []
        failed_plugins = []

        # 使用 ThreadPoolExecutor 进行并发安装
        total_start_time = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(install_plugin, plugin): plugin
                for plugin in plugins_to_install
            }
            for future in as_completed(futures):
                plugin = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error(f"插件 {plugin.plugin_name} 安装过程中出现异常: {exc}")

        total_elapsed_time = time.time() - total_start_time
        logger.info(
            f"第三方插件安装完成，成功：{len(sync_plugins)} 个，"
            f"失败：{len(failed_plugins)} 个，总耗时：{total_elapsed_time:.2f} 秒"
        )
        return sync_plugins

    @staticmethod
    def install_plugin_missing_dependencies() -> List[str]:
        """
        安装插件中缺失或不兼容的依赖项
        """
        dependency_installer = get_plugin_system().dependency
        # 第一步：获取需要安装的依赖项列表
        missing_dependencies = dependency_installer.find_missing()
        if not missing_dependencies:
            return missing_dependencies
        logger.debug(f"检测到缺失的依赖项: {missing_dependencies}")
        logger.info(f"开始安装缺失的依赖项，共 {len(missing_dependencies)} 个...")
        # 第二步：安装依赖项并返回结果
        total_start_time = time.time()
        success, message = dependency_installer.install(missing_dependencies)
        total_elapsed_time = time.time() - total_start_time
        if success:
            logger.info(f"已完成 {len(missing_dependencies)} 个依赖项安装，总耗时：{total_elapsed_time:.2f} 秒")
        else:
            logger.warning(f"存在缺失依赖项安装失败，请尝试手动安装，总耗时：{total_elapsed_time:.2f} 秒")
        return missing_dependencies

    def get_plugin_config(self, pid: str) -> dict:
        """
        获取插件配置
        :param pid: 插件ID
        """
        if not self._plugins.get(pid):
            return {}
        conf = get_plugin_storage().read(self._config_key % pid)
        if conf:
            # 去掉空Key
            return {k: v for k, v in conf.items() if k}
        return {}

    def save_plugin_config(self, pid: str, conf: dict, force: bool = False) -> bool:
        """
        保存插件配置
        :param pid: 插件ID
        :param conf: 配置
        :param force: 强制保存
        """
        if not force and not self._plugins.get(pid):
            return False
        get_plugin_storage().write(self._config_key % pid, conf)
        return True

    async def async_save_plugin_config(
        self, pid: str, conf: dict, force: bool = False
    ) -> bool:
        """
        异步保存插件配置。
        :param pid: 插件ID
        :param conf: 配置
        :param force: 强制保存
        """
        if not force and not self._plugins.get(pid):
            return False
        await get_plugin_storage().async_write(self._config_key % pid, conf)
        return True

    def delete_plugin_config(self, pid: str, force: bool = False) -> bool:
        """
        删除插件配置
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化配置
        """
        if not force and not self._plugins.get(pid):
            return False
        return get_plugin_storage().delete(self._config_key % pid)

    def delete_plugin_data(self, pid: str, force: bool = False) -> bool:
        """
        删除插件数据
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化数据
        """
        if not force and not self._plugins.get(pid):
            return False
        get_plugin_storage().delete_data(pid)
        # 删除插件数据时才销毁其数据库文件，这是不可逆操作
        _plugin_database_destroy(pid, DEFAULT_INSTANCE_ID)
        return True

    def get_plugin_state(self, pid: str) -> bool:
        """
        获取插件状态
        :param pid: 插件ID
        """
        plugin = self._plugin_registry.instance(pid)
        return plugin.get_state() if plugin else False

    def _plugin_projection(self) -> PluginProjection:
        """构造绑定当前运行态插件注册表的能力投影服务。"""
        return PluginProjection(
            self._plugin_registry.running,
            logger,
            self.get_plugin_remote_entry,
        )

    def _sync_channel_capabilities(self, plugin_id: str) -> None:
        """按插件当前声明重建其在渠道能力管理器中的登记。

        :param plugin_id: 插件 ID
        :return: 无返回值
        """
        try:
            declared = self._plugin_projection().channel_capabilities(plugin_id)
            ChannelCapabilityManager.register_extension_capabilities(
                plugin_id, declared.get(plugin_id, [])
            )
        except Exception as error:
            logger.error(f"同步插件 {plugin_id} 渠道能力登记出错：{str(error)}")

    @staticmethod
    def _revoke_channel_capabilities(plugin_id: str) -> None:
        """撤销插件在渠道能力管理器中的登记。

        插件停止后其启用状态声明不再可信，须直接清空登记，不能依赖
        重新查询插件当前声明的同步路径。

        :param plugin_id: 插件 ID
        :return: 无返回值
        """
        try:
            ChannelCapabilityManager.register_extension_capabilities(plugin_id, [])
        except Exception as error:
            logger.error(f"撤销插件 {plugin_id} 渠道能力登记出错：{str(error)}")

    def _plugin_catalog(self) -> Any:
        """构造绑定当前市场客户端和插件 DTO 映射器的目录应用服务。"""
        return _plugin_catalog_factory(self)

    def get_plugin_commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件命令
        [{
            "cmd": "/xx",
            "event": EventType.xx,
            "desc": "xxxx",
            "data": {},
            "pid": "",
        }]
        """
        return self._plugin_projection().commands(pid)

    def get_plugin_apis(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API名称",
            "description": "API说明",
            "allow_anonymous": false
        }]
        """
        return self._plugin_projection().apis(pid)

    def get_plugin_services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron、interval、date、CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数,
            "func_kwargs": {} # 方法参数
        }]
        """
        return self._plugin_projection().services(pid)

    def get_plugin_modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """
        获取插件模块
        {
            plugin_id: {
                method: function
            }
        }
        """
        return self._plugin_projection().modules(pid)

    def get_plugin_actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件动作
        [{
            "id": "动作ID",
            "name": "动作名称",
            "func": self.xxx,
            "kwargs": {} # 需要附加传递的参数
        }]
        """
        return self._plugin_projection().actions(pid)

    @staticmethod
    def _copy_plugin_agent_tools(
        tools_info: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        复制插件智能体工具注册信息，避免调用方修改缓存内容。
        """
        return [
            {
                **plugin_info,
                "tools": list(plugin_info.get("tools", [])),
            }
            for plugin_info in tools_info
        ]

    def get_plugin_agent_tools(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件智能体工具
        [{
            "plugin_id": "插件ID",
            "plugin_name": "插件名称",
            "tools": [ToolClass1, ToolClass2, ...]
        }]
        """
        cache_key = pid or "__all__"
        for _attempt in range(self.AGENT_TOOLS_BUILD_MAX_ATTEMPTS):
            with self._plugin_agent_tools_cache_lock:
                cache_revision = self._plugin_agent_tools_revision
                cached_tools = self._plugin_agent_tools_cache.get(cache_key)
            if cached_tools is not None:
                return self._copy_plugin_agent_tools(cached_tools)

            ret_tools = []
            # 创建字典快照避免并发修改
            running_plugins_snapshot = dict(self._running_plugins)
            for plugin_id, plugin in running_plugins_snapshot.items():
                if pid and pid != plugin_id:
                    continue
                if supports_extension_hook(plugin, "get_agent_tools"):
                    try:
                        if not plugin.get_state():
                            continue
                        tools = plugin.get_agent_tools()
                        if tools:
                            ret_tools.append({
                                "plugin_id": plugin_id,
                                "plugin_name": plugin.plugin_name,
                                "tools": tools
                            })
                    except Exception as e:
                        logger.error(f"获取插件 {plugin_id} 智能体工具出错：{str(e)}")
            with self._plugin_agent_tools_cache_lock:
                if cache_revision != self._plugin_agent_tools_revision:
                    # 插件状态在注册表构建期间发生变化，重新读取以避免写回过期快照。
                    continue
                self._plugin_agent_tools_cache[cache_key] = self._copy_plugin_agent_tools(
                    ret_tools
                )
                return ret_tools
        raise RuntimeError("插件工具注册表持续变化，无法建立当前快照")

    @staticmethod
    def get_plugin_remote_entry(plugin_id: str, dist_path: str) -> str:
        """
        获取插件的远程入口地址
        :param plugin_id: 插件 ID
        :param dist_path: 插件的分发路径
        :return: 远程入口地址
        """
        dist_path = dist_path.strip("/")
        path = posixpath.join(
            "plugin",
            "file",
            plugin_id.lower(),
            dist_path,
            "remoteEntry.js",
        )
        if not path.startswith("/"):
            path = "/" + path
        return path

    def get_plugin_remotes(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件联邦组件列表
        """
        return self._plugin_projection().remotes(pid)

    def get_plugin_auth_providers(self) -> List[Dict[str, Any]]:
        """
        聚合插件声明的登录认证提供方。

        :return: 插件认证入口列表
        """
        return self._plugin_projection().auth_providers()

    def get_plugin_sidebar_nav(self) -> List[Dict[str, Any]]:
        """
        聚合所有已启用 Vue 插件的侧栏导航项（get_sidebar_nav）。
        """
        return self._plugin_projection().sidebar()

    def get_plugin_dashboard_meta(self) -> List[Dict[str, str]]:
        """
        获取所有插件仪表盘元信息
        """
        return self._plugin_projection().dashboard_metadata()

    def get_plugin_dashboard(self, pid: str, key: str, user_agent: str = None) -> Optional[_SchemaPluginDashboard]:
        """
        获取插件仪表盘
        """

        def __get_params_count(func: Callable):
            """
            获取函数的参数信息
            """
            signature = inspect.signature(func)
            return len(signature.parameters)

        # 获取插件实例
        plugin_instance = self._plugin_registry.instance(pid)
        if not plugin_instance:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"插件 {pid} 不存在或未加载")

        # 渲染模式
        render_mode, _ = plugin_instance.get_render_mode()
        # 获取插件仪表板
        try:
            # 检查方法的参数个数
            params_count = __get_params_count(plugin_instance.get_dashboard)
            if params_count > 1:
                dashboard: Tuple = plugin_instance.get_dashboard(key=key, user_agent=user_agent)
            elif params_count > 0:
                dashboard: Tuple = plugin_instance.get_dashboard(user_agent=user_agent)
            else:
                dashboard: Tuple = plugin_instance.get_dashboard()
        except Exception as e:
            logger.error(f"插件 {pid} 调用方法 get_dashboard 出错: {str(e)}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"插件 {pid} 调用方法 get_dashboard 出错: {str(e)}")
        if dashboard is None:
            return None
        if not isinstance(dashboard, (tuple, list)) or len(dashboard) != 3:
            logger.error(f"插件 {pid} 返回的仪表盘数据格式错误")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"插件 {pid} 返回的仪表盘数据格式错误")
        cols, attrs, elements = dashboard
        return _SchemaPluginDashboard(
            id=pid,
            name=plugin_instance.plugin_name,
            key=key,
            render_mode=render_mode,
            cols=cols or {},
            attrs=attrs or {},
            elements=elements
        )

    def get_plugin_attr(self, pid: str, attr: str) -> Any:
        """
        获取插件属性
        :param pid: 插件ID
        :param attr: 属性名
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, attr):
            return None
        return getattr(plugin, attr)

    def run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        运行插件方法
        :param pid: 插件ID
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        return getattr(plugin, method)(*args, **kwargs)

    async def async_run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        异步运行插件方法
        :param pid: 插件ID
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        """
        plugin = self._plugin_registry.instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        method_func = getattr(plugin, method)
        if asyncio.iscoroutinefunction(method_func):
            return await method_func(*args, **kwargs)
        else:
            return method_func(*args, **kwargs)

    def get_plugin_ids(self) -> List[str]:
        """
        获取所有插件ID
        """
        return self._plugin_registry.plugin_ids()

    def get_running_plugin_ids(self) -> List[str]:
        """
        获取所有运行态插件ID
        """
        return self._plugin_registry.running_ids()

    def get_online_plugins(self, force: bool = False) -> List[_SchemaPlugin]:
        """
        获取所有在线插件信息
        """
        if not settings.PLUGIN_MARKET:
            return []
        compatible_flags = get_plugin_system().compatible_flags(
            settings.VERSION_FLAG
        )
        markets = [m for m in settings.PLUGIN_MARKET.split(",") if m]
        result = self._plugin_catalog().collect(
            markets=markets,
            compatible_flags=compatible_flags,
            force=force,
            loader=self.get_plugins_from_market,
        )
        logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    def get_local_plugins(self) -> List[_SchemaPlugin]:
        """
        获取所有本地已下载的插件信息
        """
        # 返回值
        plugins = []
        # 已安装插件
        installed_apps = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        for pid, plugin_class in self._plugins.items():
            # 运行状插件
            plugin_obj = self._running_plugins.get(pid)
            # 基本属性
            plugin = _SchemaPlugin()
            # ID
            plugin.id = pid
            # 安装状态
            if pid in installed_apps:
                plugin.installed = True
            else:
                plugin.installed = False
            # 运行状态
            if plugin_obj and hasattr(plugin_obj, "get_state"):
                try:
                    state = plugin_obj.get_state()
                except Exception as e:
                    logger.error(f"获取插件 {pid} 状态出错：{str(e)}")
                    state = False
                plugin.state = state
            else:
                plugin.state = False
            # 是否有详情页面
            if hasattr(plugin_class, "get_page"):
                plugin.has_page = supports_extension_hook(plugin_class, "get_page")
            # 公钥
            if hasattr(plugin_class, "plugin_public_key"):
                plugin.plugin_public_key = plugin_class.plugin_public_key
            # 权限
            if not self.__set_and_check_auth_level(plugin=plugin, source=plugin_class):
                continue
            # 名称
            if hasattr(plugin_class, "plugin_name"):
                plugin.plugin_name = plugin_class.plugin_name
            # 描述
            if hasattr(plugin_class, "plugin_desc"):
                plugin.plugin_desc = plugin_class.plugin_desc
            # 版本
            if hasattr(plugin_class, "plugin_version"):
                plugin.plugin_version = plugin_class.plugin_version
            # 图标
            if hasattr(plugin_class, "plugin_icon"):
                plugin.plugin_icon = plugin_class.plugin_icon
            # 作者
            if hasattr(plugin_class, "plugin_author"):
                plugin.plugin_author = plugin_class.plugin_author
            # 作者链接
            if hasattr(plugin_class, "author_url"):
                plugin.author_url = plugin_class.author_url
            # 加载顺序
            if hasattr(plugin_class, "plugin_order"):
                plugin.plugin_order = plugin_class.plugin_order
            # 是否需要更新
            plugin.has_update = False
            # 本地标志
            plugin.is_local = True
            # 汇总
            plugins.append(plugin)
        # 根据加载排序重新排序
        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        return plugins

    def get_local_plugin_version(self, pid: str) -> Optional[str]:
        """
        获取指定已安装插件的本地版本，不触发全部插件的状态、页面和权限计算。

        插件类由运行期动态加载，旧插件可能未声明版本属性，因此缺失时返回 None。
        """
        installed_apps = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        if pid not in installed_apps:
            return None
        # 保留测试和旧扩展可能替换 `_plugins` 字典的兼容接缝。
        plugin_class = self._plugins.get(pid)
        if not plugin_class:
            return None
        return getattr(plugin_class, "plugin_version", None)

    def get_local_repo_plugins(self) -> List[_SchemaPlugin]:
        """
        获取本地插件仓库目录中的插件信息
        """
        plugins = []
        installed_apps = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        local_candidates = get_plugin_system().local_candidates()
        if not local_candidates:
            return []
        for pid, plugin_info in local_candidates.items():
            package_version = plugin_info.get("package_version")
            plugin = self._process_plugin_info(
                pid=pid,
                plugin_info=plugin_info,
                market=get_plugin_system().local_repo_url(
                    pid,
                    plugin_info.get("repo_path"),
                    package_version
                ),
                installed_apps=installed_apps,
                add_time=0,
                package_version=package_version
            )
            if not plugin:
                continue
            plugin.is_local = True
            plugins.append(plugin)

        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        logger.info(f"获取到 {len(plugins)} 个本地插件")
        return plugins

    @staticmethod
    def is_plugin_exists(pid: str, version: str = None) -> bool:
        """
        判断插件是否存在，并满足版本要求(有传入version时)
        :param pid: 插件ID
        :param version: 插件版本
        """
        if not pid:
            return False
        try:
            # 构建包名
            package_name = f"app.plugins.{pid.lower()}"
            # 检查包是否存在
            spec = importlib.util.find_spec(package_name)
            package_exists = spec is not None and spec.origin is not None
            logger.debug(f"{pid} exists: {package_exists}")
            if not package_exists:
                return False

            local_version = PluginManager().get_plugin_attr(pid=pid, attr="plugin_version")
            if not local_version:
                return False

            if version and not compare_version(local_version, ">=", version):
                logger.warn(f"Plugin {pid} version: {local_version} (older than version: {version})")
                return False

            return True
        except Exception as e:
            logger.debug(f"获取插件是否在本地包中存在失败，{e}")
            return False

    def get_plugins_from_market(self, market: str,
                                package_version: Optional[str] = None,
                                force: bool = False) -> Optional[List[_SchemaPlugin]]:
        """
        从指定的市场获取插件信息
        :param market: 市场的 URL 或标识
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        :param force: 是否强制刷新（忽略缓存）
        :return: 返回插件的列表，若获取失败返回 []
        """
        return self._plugin_catalog().load(market, package_version, force)

    def process_plugins_list(self, higher_version_plugins: List[_SchemaPlugin],
                             base_version_plugins: List[_SchemaPlugin]) -> List[_SchemaPlugin]:
        """
        处理插件列表：合并、去重、排序、保留最高版本
        :param higher_version_plugins: 高版本插件列表
        :param base_version_plugins: 基础版本插件列表
        :return: 处理后的插件列表
        """
        markets = [item for item in settings.PLUGIN_MARKET.split(",") if item]
        return self._plugin_catalog().merge(
            higher_version_plugins,
            base_version_plugins,
            markets,
        )

    def _process_plugin_info(self, pid: str, plugin_info: dict, market: str,
                             installed_apps: List[str], add_time: int,
                             package_version: Optional[str] = None) -> Optional[_SchemaPlugin]:
        """
        处理单个插件信息，创建 schemas.Plugin 对象
        :param pid: 插件ID
        :param plugin_info: 插件信息字典
        :param market: 市场URL
        :param installed_apps: 已安装插件列表
        :param add_time: 添加顺序
        :param package_version: 包版本
        :return: 创建的插件对象，如果验证失败返回None
        """
        if not isinstance(plugin_info, dict):
            return None

        plugin_info = get_plugin_system().annotate_system_version(
            plugin_info.copy()
        )
        if not get_plugin_system().is_package_compatible(
                plugin_info, package_version or ""
        ):
            # 插件当前版本不兼容
            return None

        # 运行状插件
        plugin_obj = self._plugin_registry.instance(pid)
        # 非运行态插件
        plugin_static = self._plugin_registry.plugin_class(pid)
        # 基本属性
        plugin = _SchemaPlugin()
        # ID
        plugin.id = pid
        # 安装状态
        if pid in installed_apps and plugin_static:
            plugin.installed = True
        else:
            plugin.installed = False
        # 是否有新版本
        plugin.has_update = False
        if plugin_static:
            installed_version = getattr(plugin_static, "plugin_version")
            if compare_version(installed_version, "<", plugin_info.get("version")):
                # 需要更新
                plugin.has_update = True
        # 主系统版本兼容性
        if plugin_info.get("system_version"):
            plugin.system_version = plugin_info.get("system_version")
        if plugin_info.get("system_version_compatible") is False:
            plugin.system_version_compatible = False
            plugin.system_version_message = plugin_info.get("system_version_message")
        # 运行状态
        if plugin_obj and hasattr(plugin_obj, "get_state"):
            try:
                state = plugin_obj.get_state()
            except Exception as e:
                logger.error(f"获取插件 {pid} 状态出错：{str(e)}")
                state = False
            plugin.state = state
        else:
            plugin.state = False
        # 是否有详情页面
        plugin.has_page = False
        if plugin_obj and supports_extension_hook(plugin_obj, "get_page"):
            plugin.has_page = True
        # 公钥
        if plugin_info.get("key"):
            plugin.plugin_public_key = plugin_info.get("key")
        # 权限
        if not self.__set_and_check_auth_level(plugin=plugin, source=plugin_info):
            return None
        # 名称
        if plugin_info.get("name"):
            plugin.plugin_name = plugin_info.get("name")
        # 描述
        if plugin_info.get("description"):
            plugin.plugin_desc = plugin_info.get("description")
        # 版本
        if plugin_info.get("version"):
            plugin.plugin_version = plugin_info.get("version")
        # 图标
        if plugin_info.get("icon"):
            plugin.plugin_icon = plugin_info.get("icon")
        # 标签
        plugin.plugin_label = self._normalize_plugin_label(plugin_info.get("labels"))
        # 作者
        if plugin_info.get("author"):
            plugin.plugin_author = plugin_info.get("author")
        # 更新历史
        if plugin_info.get("history"):
            plugin.history = plugin_info.get("history")
        # Release 能力位来自插件市场索引，用于前端展示和后端安装入口双重校验。
        plugin.release = bool(plugin_info.get("release"))
        # 仓库链接
        plugin.repo_url = market
        # 本地标志
        plugin.is_local = False
        # 添加顺序
        plugin.add_time = add_time

        return plugin

    @staticmethod
    def _normalize_plugin_label(labels: Any) -> Optional[str]:
        """
        规整插件市场标签字段，兼容旧字符串和新列表格式。

        :param labels: 插件市场 package 中的 labels 字段
        :return: 用空格拼接后的标签字符串，无法识别或为空时返回 None
        """
        if isinstance(labels, str):
            label = labels.strip()
            return label or None
        if isinstance(labels, list):
            normalized_labels = [str(item).strip() for item in labels if str(item).strip()]
            return " ".join(normalized_labels) or None
        return None

    async def async_get_online_plugins(
            self,
            force: bool = False,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> List[_SchemaPlugin]:
        """
        异步获取所有在线插件信息
        :param force: 是否强制刷新（忽略缓存）
        :param progress_callback: 定时服务进度更新回调
        """
        if not settings.PLUGIN_MARKET:
            if progress_callback:
                progress_callback(value=100, text="未配置插件市场，跳过刷新")
            return []
        compatible_flags = get_plugin_system().compatible_flags(
            settings.VERSION_FLAG
        )
        result = await self._plugin_catalog().async_collect(
            markets=[item for item in settings.PLUGIN_MARKET.split(",") if item],
            compatible_flags=compatible_flags,
            force=force,
            loader=self.async_get_plugins_from_market,
            progress_callback=progress_callback,
        )
        logger.info(f"获取到 {len(result)} 个线上插件")
        return result

    async def async_get_plugins_from_market(self, market: str,
                                            package_version: Optional[str] = None,
                                            force: bool = False) -> Optional[List[_SchemaPlugin]]:
        """
        异步从指定的市场获取插件信息
        :param market: 市场的 URL 或标识
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        :param force: 是否强制刷新（忽略缓存）
        :return: 返回插件的列表，若获取失败返回 []
        """
        return await self._plugin_catalog().async_load(
            market,
            package_version,
            force,
        )

    @staticmethod
    def __set_and_check_auth_level(plugin: Union[_SchemaPlugin, Type[Any]],
                                   source: Optional[Union[dict, Type[Any]]] = None) -> bool:
        """
        设置并检查插件的认证级别
        :param plugin: 插件对象或包含 auth_level 属性的对象
        :param source: 可选的字典对象或类对象，可能包含 "level" 或 "auth_level" 键
        :return: 如果插件的认证级别有效且当前环境的认证级别满足要求，返回 True，否则返回 False
        """
        # 检查并赋值 source 中的 level 或 auth_level
        if source:
            if isinstance(source, dict) and "level" in source:
                plugin.auth_level = source.get("level")
            elif hasattr(source, "auth_level"):
                plugin.auth_level = source.auth_level
        # 如果 source 为空且 plugin 本身没有 auth_level，直接返回 True
        elif not hasattr(plugin, "auth_level"):
            return True

        # auth_level 级别说明
        # 1 - 所有用户可见
        # 2 - 站点认证用户可见
        # 3 - 站点&密钥认证可见
        # 99 - 站点&特殊密钥认证可见
        # 如果当前站点认证级别大于 1 且插件级别为 99，并存在插件公钥，说明为特殊密钥认证，通过密钥匹配进行认证
        auth_level = _site_auth_level_provider()
        if auth_level > 1 and plugin.auth_level == 99 and hasattr(plugin, "plugin_public_key"):
            plugin_id = plugin.id if isinstance(plugin, _SchemaPlugin) else plugin.__name__
            public_key = plugin.plugin_public_key
            if public_key:
                private_key = PluginManager.__get_plugin_private_key(plugin_id)
                verify = RSAUtils.verify_rsa_keys(public_key=public_key, private_key=private_key)
                return verify
        # 如果当前站点认证级别小于插件级别，则返回 False
        if auth_level < plugin.auth_level:
            return False
        return True

    @staticmethod
    def __get_plugin_private_key(plugin_id: str) -> Optional[str]:
        """
        根据插件标识获取对应的私钥
        :param plugin_id: 插件标识
        :return: 对应的插件私钥，如果未找到则返回 None
        """
        try:
            # 将插件标识转换为大写并构建环境变量名称
            env_var_name = f"PLUGIN_{plugin_id.upper()}_PRIVATE_KEY"
            private_key = os.environ.get(env_var_name)
            return private_key
        except Exception as e:
            logger.debug(f"获取插件 {plugin_id} 的私钥时发生错误：{e}")
            return None

    def clone_plugin(self, plugin_id: str, suffix: str, name: str, description: str,
                     version: str = None, icon: str = None) -> Tuple[bool, str]:
        """
        创建插件分身
        :param plugin_id: 原插件ID
        :param suffix: 分身后缀
        :param name: 分身名称
        :param description: 分身描述
        :param version: 自定义版本号
        :param icon: 自定义图标URL
        :return: (是否成功, 错误信息)
        """
        try:
            # 验证参数
            if not plugin_id or not suffix:
                return False, "插件ID和分身后缀不能为空"

            # 检查原插件是否存在
            if plugin_id not in self._plugins:
                return False, f"原插件 {plugin_id} 不存在"

            # 生成分身插件ID
            clone_id = f"{plugin_id}{suffix.lower()}"

            # 检查分身插件是否已存在
            if self.is_plugin_exists(clone_id):
                return False, f"分身插件 {clone_id} 已存在"

            original_plugin_class = self._plugins.get(plugin_id)
            if not original_plugin_class:
                return False, f"无法获取原插件类 {plugin_id}"

            success, msg = get_plugin_system().package.clone(
                plugin_id=plugin_id,
                clone_id=clone_id,
                original_class_name=original_plugin_class.__name__,
                suffix=suffix.lower(),
                name=name,
                description=description,
                version=version,
                icon=icon,
            )
            if not success:
                return False, msg

            # 将分身插件添加到已安装列表
            storage = get_plugin_storage()
            installed_plugins = storage.read(SystemConfigKey.UserInstalledPlugins) or []
            if clone_id not in installed_plugins:
                installed_plugins.append(clone_id)
                storage.write(SystemConfigKey.UserInstalledPlugins, installed_plugins)

            # 为分身插件创建初始配置（从原插件复制配置）
            logger.info(f"正在为分身插件 {clone_id} 创建初始配置...")
            original_config = self.get_plugin_config(plugin_id)
            if original_config:
                # 复制原插件配置作为分身插件的初始配置
                clone_config = original_config.copy()
                # 可以在这里修改一些默认值，比如禁用分身插件
                # 默认禁用分身插件，让用户手动配置
                clone_config['enable'] = False
                clone_config['enabled'] = False
                self.save_plugin_config(clone_id, clone_config, force=True)
                logger.info(f"已为分身插件 {clone_id} 设置初始配置")
            else:
                logger.info(f"原插件 {plugin_id} 没有配置，分身插件 {clone_id} 将使用默认配置")

            # 注册分身插件的API和服务
            logger.info(f"正在注册分身插件 {clone_id} ...")
            PluginManager().reload_plugin(clone_id)
            # 确保分身插件正确初始化配置
            if clone_id in self._running_plugins:
                clone_instance = self._running_plugins[clone_id]
                clone_config = self.get_plugin_config(clone_id)
                if clone_config:
                    logger.info(f"正在为分身插件 {clone_id} 重新初始化配置...")
                    clone_instance.init_plugin(clone_config)
                    logger.info(f"分身插件 {clone_id} 配置重新初始化完成")

            logger.info(f"插件分身 {clone_id} 创建成功")
            return True, clone_id

        except Exception as e:
            logger.error(f"创建插件分身失败：{str(e)}")
            return False, f"创建插件分身失败：{str(e)}"

    def _modify_plugin_files(self, plugin_dir: Path, original_id: str, suffix: str,
                             name: str, description: str, version: str = None,
                             icon: str = None) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将分身文件改写委托给包适配器。
        :param plugin_dir: 插件目录
        :param original_id: 原插件ID
        :param suffix: 分身后缀
        :param name: 分身名称
        :param description: 分身描述
        :param version: 自定义版本号
        :param icon: 自定义图标URL
        :return: (是否成功, 错误信息)
        """
        original_plugin_class = self._plugins.get(original_id)
        if not original_plugin_class:
            return False, f"无法获取原插件类 {original_id}"
        return get_plugin_system().package._modify_plugin_files(
            plugin_dir=plugin_dir,
            original_class_name=original_plugin_class.__name__,
            suffix=suffix,
            name=name,
            description=description,
            version=version,
            icon=icon,
        )

    @staticmethod
    def _modify_python_file(file_path: Path, original_class_name: str,
                            clone_class_name: str, name: str, description: str,
                            version: str = None, icon: str = None) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将 Python 文件改写委托给包适配器。
        """
        return get_plugin_system().package._modify_python_file(
            file_path=file_path,
            original_class_name=original_class_name,
            clone_class_name=clone_class_name,
            name=name,
            description=description,
            version=version,
            icon=icon,
        )

    def _modify_federation_files(self, dist_dir: Path, original_class_name: str,
                                 clone_class_name: str) -> Tuple[bool, str]:
        """
        兼容旧内部调用，将联邦文件改写委托给包适配器。
        """
        return get_plugin_system().package._modify_federation_files(
            dist_dir=dist_dir,
            original_class_name=original_class_name,
            clone_class_name=clone_class_name,
        )

    @staticmethod
    def _rename_federation_assets(dist_dir: Path, original_class_name: str, clone_class_name: str):
        """
        兼容旧内部调用，将资源重命名委托给包适配器。
        """
        get_plugin_system().package._rename_federation_assets(
            dist_dir,
            original_class_name,
            clone_class_name,
        )
