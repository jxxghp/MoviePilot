import ast
import asyncio
import importlib.util
import inspect
import os
import posixpath
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Type, Union, Callable, Tuple

from fastapi import HTTPException
from starlette import status
from watchfiles import watch

from app.schemas.plugin import Plugin as _SchemaPlugin
from app.schemas.plugin import PluginDashboard as _SchemaPluginDashboard
from app.schemas.plugin import PluginRuntimeStatus
from app.foundation.crypto import RSAUtils
from app.foundation.paths import ensure_path_segment
from app.foundation.singleton import Singleton
from app.foundation.version import compare_version
from app.runtime.log import bind_plugin_instance, logger
from app.runtime.config import settings
from app.runtime.events import Event, EventHandlerBinding, eventmanager
from app.runtime.reload import ConfigReloadMixin
from app.runtime.deprecation.policy import is_active as deprecation_is_active
from app.runtime.deprecation.policy import warn as deprecation_warn
from app.runtime.extensions.contract.extension import ExtensionDistribution, supports_extension_hook
from app.runtime.extensions.contract.declaration import (
    declaration_config_component,
    declaration_config_form,
    declaration_config_schema,
    declaration_impl,
    declaration_meta_parser_identity,
    declaration_dashboard_identity,
    declaration_meta_parser_priority,
    declaration_service_instance_constructor,
    declaration_service_instance_icon,
    declaration_service_instance_identity,
    declaration_service_instance_multi_instance,
    declaration_service_instance_requirement,
)
from app.runtime.extensions.contract.instance import (
    DEFAULT_INSTANCE_ID,
    extension_id_of,
    instance_key,
    matches_extension,
    normalize_instance_id,
    split_instance_key,
)
from app.runtime.extensions.admission.instance_selection import resolve_plugin_instance_key
from app.runtime.extensions.lifecycle.layout import (
    ensure_plugin_version_dir_available,
    plugin_module_name,
    plugin_version_dirs,
    plugin_version_from_dir_name,
    read_plugin_versions_manifest,
    recycle_plugin_version_directories,
    register_plugin_version,
    resolve_plugin_version_dir,
)
from app.runtime.extensions.projection.plugin import PluginExtension, PluginProjection
from app.runtime.extensions.registry.plugin import PluginRegistry
from app.runtime.extensions.lifecycle.storage import get_plugin_storage
from app.runtime.extensions.lifecycle.system import get_plugin_system
from app.runtime.extensions.registry.command import plugin_command_registry
from app.runtime.extensions.registry.filter_rule import plugin_filter_rule_registry
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.runtime.extensions.registry.meta_parser import meta_parser_registry
from app.runtime.extensions.service_config import (
    STORAGE_CAPABILITY,
    select_instance_configs,
    service_capability_configs,
)
from app.runtime.extensions.admission.service_instance_requirement import (
    SERVICE_INSTANCE_PARAM,
    accepts_keyword,
    resolve_required_service_instance,
)
from app.runtime.extensions.registry.storage import (
    storage_backend_registry,
    storage_instance_factory,
)
from app.runtime.extensions.contract.dependency import (
    PluginDependencyClassification,
    PluginDependencyInstallResult,
)
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
PluginInstanceConfigUpserter = Callable[[str, str, dict], None]
PluginInstanceConfigDeleter = Callable[[str, str], bool]
PluginInstanceDataDeleter = Callable[[str, str], None]
PluginInstanceVersionReader = Callable[[str, str], Optional[Tuple[Optional[str], bool]]]
PluginInstanceVersionWriter = Callable[[str, str, str], None]
PluginInstanceFollowWriter = Callable[[str, str, bool], None]
PluginVersionSwitchNotifier = Callable[[str, str], None]
PluginMultiVersionProbe = Callable[[str, List[Path]], List[str]]


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


def _ignore_plugin_instance_config_upsert(_plugin_id: str, _instance_id: str, _config: dict) -> None:
    """插件实例持久化框架尚未装配时忽略实例配置写入。"""


def _ignore_plugin_instance_config_delete(_plugin_id: str, _instance_id: str) -> bool:
    """插件实例持久化框架尚未装配时报告实例配置未删除。"""
    return False


def _ignore_plugin_instance_data_delete(_plugin_id: str, _instance_id: str) -> None:
    """插件实例持久化框架尚未装配时忽略实例数据删除。"""


def _unknown_plugin_instance_version(
    _plugin_id: str, _instance_id: str
) -> Optional[Tuple[Optional[str], bool]]:
    """实例版本绑定尚未装配时报告无绑定记录。"""
    return None


def _ignore_plugin_instance_version_write(
    _plugin_id: str, _instance_id: str, _version: str
) -> None:
    """实例版本绑定尚未装配时忽略已生效版本写入。"""


def _ignore_plugin_instance_follow_write(
    _plugin_id: str, _instance_id: str, _follow: bool
) -> None:
    """实例版本绑定尚未装配时忽略跟随开关写入。"""


def _ignore_plugin_version_switch_notice(_title: str, _text: str) -> None:
    """系统消息通道尚未装配时忽略版本切换告警。"""


def _no_multi_version_blockers(_plugin_id: str, _source_dirs: List[Path]) -> List[str]:
    """插件写法体检尚未装配时不给出多版本阻断结论。"""
    return []


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
_plugin_instance_config_upsert: PluginInstanceConfigUpserter = (
    _ignore_plugin_instance_config_upsert
)
_plugin_instance_config_delete: PluginInstanceConfigDeleter = (
    _ignore_plugin_instance_config_delete
)
_plugin_instance_data_delete: PluginInstanceDataDeleter = _ignore_plugin_instance_data_delete
_plugin_instance_version_read: PluginInstanceVersionReader = _unknown_plugin_instance_version
_plugin_instance_version_write: PluginInstanceVersionWriter = (
    _ignore_plugin_instance_version_write
)
_plugin_instance_follow_write: PluginInstanceFollowWriter = (
    _ignore_plugin_instance_follow_write
)
_plugin_version_switch_notifier: PluginVersionSwitchNotifier = (
    _ignore_plugin_version_switch_notice
)
_plugin_multi_version_probe: PluginMultiVersionProbe = _no_multi_version_blockers


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


def _configure_plugin_instance_persistence(
    *,
    upsert_config: PluginInstanceConfigUpserter,
    delete_config: PluginInstanceConfigDeleter,
    delete_data: PluginInstanceDataDeleter,
) -> None:
    """由启动组合根注入按任意实例标识写删配置与数据的持久化钩子。

    PluginStorage 端口的 write_config/delete_config 只覆盖默认实例，delete_data
    也不区分实例；创建与删除插件实例需要按任意实例标识精确写删一行，这里另开
    一组钩子，避免扩展层反向依赖 DB 层。
    :param upsert_config: 按 (插件ID, 实例标识) 写入或更新一行配置
    :param delete_config: 按 (插件ID, 实例标识) 删除一行配置，返回是否命中记录
    :param delete_data: 按 (插件ID, 实例标识) 删除该实例的全部业务数据
    """
    global _plugin_instance_config_upsert, _plugin_instance_config_delete, _plugin_instance_data_delete
    _plugin_instance_config_upsert = upsert_config
    _plugin_instance_config_delete = delete_config
    _plugin_instance_data_delete = delete_data


def _configure_plugin_instance_version_binding(
    *,
    read_binding: PluginInstanceVersionReader,
    write_version: PluginInstanceVersionWriter,
    write_follow_default: PluginInstanceFollowWriter,
) -> None:
    """由启动组合根注入实例版本绑定的读写钩子。

    绑定信息落在实例配置表的两列上，扩展层不得反向依赖 DB 层，因此这里只声明
    可注入的钩子。
    :param read_binding: 按 (插件ID, 实例标识) 读取 `(已生效版本, 是否跟随默认实例)`
    :param write_version: 按 (插件ID, 实例标识) 写入已生效版本
    :param write_follow_default: 按 (插件ID, 实例标识) 写入跟随开关
    """
    global _plugin_instance_version_read, _plugin_instance_version_write
    global _plugin_instance_follow_write
    _plugin_instance_version_read = read_binding
    _plugin_instance_version_write = write_version
    _plugin_instance_follow_write = write_follow_default


def _configure_plugin_version_switch_notifier(notifier: PluginVersionSwitchNotifier) -> None:
    """由启动组合根注入版本切换失败的系统消息通道。

    :param notifier: 接收 `(标题, 正文)` 并投递系统消息的函数
    """
    global _plugin_version_switch_notifier
    _plugin_version_switch_notifier = notifier


def _configure_plugin_multi_version_probe(probe: PluginMultiVersionProbe) -> None:
    """由启动组合根注入插件多版本准入体检。

    体检实现落在兼容层，扩展层不得反向依赖，因此这里只声明可注入的钩子。
    :param probe: 接收 `(插件目录名, 源码目录列表)` 并返回阻断原因列表的函数
    """
    global _plugin_multi_version_probe
    _plugin_multi_version_probe = probe


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
        # 插件包写入期间的监控抑制计数，按插件标识引用计数
        self._monitor_suppression_lock = threading.Lock()
        self._suppressed_monitor_plugins: Dict[str, int] = {}
        # 事件总线只通过通用解析器访问运行中的插件实例。
        eventmanager.register_handler_instance_resolver(
            "plugins",
            self.resolve_event_handler_instance,
        )

    def resolve_event_handler_instance(
            self,
            owner_class: Type[Any],
    ) -> Optional[List[EventHandlerBinding]]:
        """为插件声明的事件方法解析当前运行实例绑定列表。

        :param owner_class: 声明事件处理方法的插件类
        :return: 该插件全部运行实例的绑定，每条带其实例键；非插件类返回 None
        """
        plugin_id = owner_class.__name__
        # 旧测试与部分扩展会替换私有映射来构造隔离运行态，解析器继续尊重该接缝。
        if plugin_id not in self._plugins:
            return None
        bindings: List[EventHandlerBinding] = []
        for key, plugin in dict(self._running_plugins).items():
            if extension_id_of(key) != plugin_id:
                continue
            owner_name = key
            if callable(getattr(plugin, "get_name", None)):
                owner_name = plugin.get_name()
            bindings.append(
                EventHandlerBinding(
                    instance=plugin,
                    owner_name=owner_name,
                    run_sync_in_threadpool=True,
                    instance_key=key,
                )
            )
        return bindings

    def init_config(self):
        """按最新系统配置完整重启插件。"""
        # 停止已有插件
        self.stop()
        classification = self.classify_plugins()
        self.apply_plugin_dependency_classification(classification)
        for plugin_id in classification.ready:
            self.start(plugin_id)

    def start(
        self,
        pid: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> Dict[str, PluginRuntimeStatus]:
        """
        启动加载插件
        :param pid: 插件ID，为空加载所有插件
        :param instance_id: 实例标识，为空时加载该插件已登记的全部实例
        :return: 本次涉及的插件ID到其运行状态的映射
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

        # 目标插件与目标实例：pid 传实例键时，插件目录按其所属插件标识定位
        target_plugin_id = extension_id_of(pid) if pid else None
        target_instance_id = instance_id
        if pid and not target_instance_id and pid != target_plugin_id:
            target_instance_id = split_instance_key(pid)[1]
        results: Dict[str, PluginRuntimeStatus] = {}
        # 指名加载时先落 ready，导入期间的读取方看到的是准备中而不是状态缺失
        if target_plugin_id:
            self._plugin_registry.set_runtime_status(target_plugin_id, PluginRuntimeStatus.READY)
        # 已安装插件
        installed_plugins = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        # 扫描插件目录，只加载符合条件的插件
        plugins = self._load_selective_plugins(target_plugin_id, installed_plugins, check_module)
        # 排序
        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        for plugin in plugins:
            plugin_id = plugin.__name__
            if target_plugin_id and plugin_id != target_plugin_id:
                continue
            try:
                # 判断插件是否满足认证要求，如不满足则不进行实例化
                if not self.__set_and_check_auth_level(plugin=plugin):
                    # 如果是插件热更新实例，这里则进行替换
                    if plugin_id in self._plugins:
                        self._plugins[plugin_id] = plugin
                    results[plugin_id] = self._record_runtime_status(
                        plugin_id, PluginRuntimeStatus.BLOCKED_BY_POLICY
                    )
                    continue
                # 存储Class
                self._plugins[plugin_id] = plugin
                if target_instance_id:
                    instance_ids = [normalize_instance_id(target_instance_id)]
                else:
                    instance_ids = self._plugin_instance_ids(plugin_id)
                for target in instance_ids:
                    self._start_instance_with_version(plugin, plugin_id, target)
                self._sync_family_event_state(plugin_id, plugin)
                # 一族里只要还有实例在运行就算激活，个别实例失败不改变整族状态
                results[plugin_id] = self._record_runtime_status(
                    plugin_id,
                    PluginRuntimeStatus.ACTIVE
                    if self._plugin_registry.instance_keys(plugin_id)
                    else PluginRuntimeStatus.LOAD_FAILED,
                )
            except Exception as err:
                results[plugin_id] = self._record_runtime_status(
                    plugin_id, PluginRuntimeStatus.LOAD_FAILED
                )
                logger.error(f"加载插件 {plugin_id} 出错：{str(err)} - {traceback.format_exc()}")
        # 指名加载的插件没有产出任何可用类时按加载失败对外可见
        if target_plugin_id and target_plugin_id not in results:
            results[target_plugin_id] = self._record_runtime_status(
                target_plugin_id, PluginRuntimeStatus.LOAD_FAILED
            )
        self.clear_plugin_agent_tools_cache()
        return results

    def _record_runtime_status(
        self,
        plugin_id: str,
        status: PluginRuntimeStatus,
    ) -> PluginRuntimeStatus:
        """写入插件运行状态并回传，便于调用方同时记录本轮结果。

        :param plugin_id: 插件ID
        :param status: 本次判定的运行状态
        :return: 写入的运行状态
        """
        self._plugin_registry.set_runtime_status(plugin_id, status)
        return status

    def start_instance(self, pid: str, instance_id: str) -> None:
        """
        启动指定插件的单个实例，兄弟实例不受影响
        :param pid: 插件ID
        :param instance_id: 实例标识
        """
        self.start(pid, instance_id)

    @staticmethod
    def _read_plugin_instance_ids(plugin_id: str) -> List[str]:
        """
        直读插件已登记的实例清单，读取出错直接上抛
        :param plugin_id: 插件ID
        :return: 实例标识列表；一条实例配置都没有时回落到单个默认实例
        """
        normalized = [
            normalize_instance_id(item)
            for item in get_plugin_storage().list_instances(plugin_id)
            if isinstance(item, str)
        ]
        ordered = list(dict.fromkeys(normalized)) or [DEFAULT_INSTANCE_ID]
        if DEFAULT_INSTANCE_ID in ordered:
            # 跟随默认版本的实例要读默认实例本次启动后登记的版本，默认实例必须先起
            ordered.remove(DEFAULT_INSTANCE_ID)
            ordered.insert(0, DEFAULT_INSTANCE_ID)
        return ordered

    @staticmethod
    def _plugin_instance_ids(plugin_id: str) -> List[str]:
        """
        读取插件已登记的实例清单，读取出错时回落到单个默认实例
        :param plugin_id: 插件ID
        :return: 实例标识列表；一条实例配置都没有时回落到单个默认实例
        """
        try:
            return PluginManager._read_plugin_instance_ids(plugin_id)
        except Exception as err:
            logger.error(f"读取插件 {plugin_id} 实例清单出错：{str(err)}")
            return [DEFAULT_INSTANCE_ID]

    @staticmethod
    def _instantiate_plugin(plugin_class: Type[Any], plugin_id: str, instance_id: str) -> Any:
        """
        构造插件运行实例，并回写其插件标识与实例标识
        :param plugin_class: 插件类
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: 插件运行实例
        """
        try:
            parameters = inspect.signature(plugin_class.__init__).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs = {}
        if "plugin_id" in parameters:
            kwargs["plugin_id"] = plugin_id
        if "instance_id" in parameters:
            kwargs["instance_id"] = instance_id
        plugin_obj = plugin_class(**kwargs)
        # 存量插件的无参 __init__ 不会收到这两个参数，构造后无条件回写保证属性一定存在
        plugin_obj.plugin_id = plugin_id
        plugin_obj.instance_id = instance_id
        return plugin_obj

    @staticmethod
    def _instance_version_binding(plugin_id: str, instance_id: str) -> Tuple[Optional[str], bool]:
        """
        读取实例的已生效版本与跟随开关
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: `(已生效版本, 是否跟随默认实例)`；无记录或读取出错时为 `(None, True)`
        """
        try:
            binding = _plugin_instance_version_read(plugin_id, instance_id)
        except Exception as err:
            logger.error(f"读取插件 {plugin_id} 实例 {instance_id} 版本绑定出错：{str(err)}")
            return None, True
        if not binding:
            return None, True
        version, follow = binding
        return (version or None), bool(follow)

    def _desired_instance_version(self, plugin_id: str, instance_id: str) -> Optional[str]:
        """
        解析实例本次应加载的插件版本

        跟随开关决定期望版本的来源：普通实例为真时取默认实例已生效的版本，为假时取
        本实例自己已生效的版本。默认实例没有可跟随的兄弟，其跟随开关表示跟随插件当前
        安装的版本，因此返回空让调用方按当前版本目录启动；关掉跟随即固定在自己已生效
        的版本上。
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: 期望版本；跟随插件当前版本或无从解析时为 None
        """
        version, follow = self._instance_version_binding(plugin_id, instance_id)
        if instance_id == DEFAULT_INSTANCE_ID:
            return None if follow else version
        if not follow:
            return version
        default_version, _ = self._instance_version_binding(plugin_id, DEFAULT_INSTANCE_ID)
        return default_version or version

    @staticmethod
    def _plugin_version_source_dir(plugin_id: str, version: str) -> Optional[Path]:
        """
        定位插件某个版本的源码目录
        :param plugin_id: 插件ID
        :param version: 版本号
        :return: 版本目录；该版本未落盘时为 None
        """
        plugin_root = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        return plugin_version_dirs(plugin_root).get(version)

    @staticmethod
    def _load_plugin_class_for_version(
        plugin_id: str, version: str, source_dir: Path
    ) -> Optional[Type[Any]]:
        """
        按指定版本目录导入插件模块并取出其主类
        :param plugin_id: 插件ID
        :param version: 版本号
        :param source_dir: 该版本的源码目录
        :return: 插件类；导入失败或模块内没有插件类时为 None
        """
        plugin_root = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        module_name = plugin_module_name(plugin_root, source_dir)
        try:
            module = importlib.import_module(module_name)
        except Exception as err:
            logger.error(
                f"导入插件 {plugin_id} 版本 {version} 的模块 {module_name} 失败："
                f"{str(err)} - {traceback.format_exc()}"
            )
            return None
        candidates = [
            obj
            for name, obj in module.__dict__.items()
            if not name.startswith("_")
            and isinstance(obj, type)
            and hasattr(obj, "init_plugin")
            and hasattr(obj, "plugin_name")
        ]
        for candidate in candidates:
            if candidate.__name__ == plugin_id:
                return candidate
        if candidates:
            return candidates[0]
        logger.error(f"插件 {plugin_id} 版本 {version} 的模块 {module_name} 中没有插件类")
        return None

    @staticmethod
    def _record_effective_version(plugin_id: str, instance_id: str, version: str) -> None:
        """
        把本次成功启动的版本登记为该实例的已生效版本
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :param version: 本次生效的版本号
        """
        try:
            _plugin_instance_version_write(plugin_id, instance_id, version)
        except Exception as err:
            logger.error(
                f"登记插件实例 {instance_key(plugin_id, instance_id)} 已生效版本 "
                f"{version} 出错：{str(err)}"
            )

    def _start_and_record(
        self,
        plugin_class: Type[Any],
        plugin_id: str,
        instance_id: str,
        version: Optional[str],
        effective_version: Optional[str],
    ) -> bool:
        """
        启动实例，成功后才登记本次已生效的版本
        :param plugin_class: 本次使用的插件类
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :param version: 本次尝试启动的版本号
        :param effective_version: 启动前登记的已生效版本
        :return: 实例是否成功进入运行态
        """
        if not self.__start_instance(plugin_class, plugin_id, instance_id):
            return False
        if version and version != effective_version:
            self._record_effective_version(plugin_id, instance_id, version)
        return True

    def _recover_previous_version(
        self,
        plugin_class: Type[Any],
        plugin_id: str,
        instance_id: str,
        desired: str,
        effective_version: Optional[str],
        current_version: Optional[str],
    ) -> bool:
        """
        目标版本启动失败后以已生效版本重新启动，完成回退

        已生效版本一列保持原值不动，其版本目录仍在磁盘上，因此可以直接以该版本
        重启；回退期间不写入任何版本，兄弟实例不受影响。
        :param plugin_class: 插件当前版本的类
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :param desired: 启动失败的目标版本
        :param effective_version: 启动前登记的已生效版本
        :param current_version: 插件当前版本目录对应的版本号
        :return: 回退启动是否成功
        """
        key = instance_key(plugin_id, instance_id)
        if not effective_version or effective_version == desired:
            logger.error(f"插件实例 {key} 以版本 {desired} 启动失败，且没有可回退的已生效版本")
            return False
        logger.error(
            f"插件实例 {key} 切换到版本 {desired} 失败，已生效版本 {effective_version} "
            f"保持不变，正在以该版本重新启动"
        )
        try:
            _plugin_version_switch_notifier(
                "插件版本切换失败",
                f"插件实例 {key} 切换到版本 {desired} 失败，已回退到版本 "
                f"{effective_version}，请查看插件日志排查原因。",
            )
        except Exception as err:
            logger.error(f"发送插件 {key} 版本切换失败消息出错：{str(err)}")
        if effective_version == current_version:
            fallback_class = plugin_class
        else:
            source_dir = self._plugin_version_source_dir(plugin_id, effective_version)
            fallback_class = (
                self._load_plugin_class_for_version(plugin_id, effective_version, source_dir)
                if source_dir is not None
                else None
            )
        if fallback_class is not None and self.__start_instance(
            fallback_class, plugin_id, instance_id
        ):
            return True
        logger.error(f"插件实例 {key} 以已生效版本 {effective_version} 回退启动同样失败")
        return False

    def _start_instance_with_version(
        self,
        plugin_class: Type[Any],
        plugin_id: str,
        instance_id: str,
        requested_version: Optional[str] = None,
    ) -> bool:
        """
        按实例绑定的版本启动单个实例，切换失败时回退到已生效版本

        三种异常情形各自的行为：实例从未成功启动过（无已生效版本）时按插件当前
        版本启动并登记；绑定的版本目录不存在时告警并回落到插件当前版本，启动成功
        后如实登记该版本；目标版本加载或启动失败时保持已生效版本不变，再以该版本
        重新启动完成回退。任一情形都只影响本实例。
        :param plugin_class: 插件当前版本的类
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :param requested_version: 本次显式指定的目标版本，为空时按绑定解析
        :return: 实例是否成功进入运行态
        """
        key = instance_key(plugin_id, instance_id)
        current_version = getattr(plugin_class, "plugin_version", None)
        effective_version, _follow = self._instance_version_binding(plugin_id, instance_id)
        desired = requested_version or self._desired_instance_version(plugin_id, instance_id)

        if not desired:
            logger.info(f"插件实例 {key} 未固定版本，按插件当前版本 {current_version} 启动")
            return self._start_and_record(
                plugin_class, plugin_id, instance_id, current_version, effective_version
            )

        source_dir = (
            None if desired == current_version
            else self._plugin_version_source_dir(plugin_id, desired)
        )
        if desired != current_version and source_dir is None:
            logger.warning(
                f"插件实例 {key} 绑定的版本 {desired} 的版本目录不存在，"
                f"回落到插件当前版本 {current_version} 启动"
            )
            return self._start_and_record(
                plugin_class, plugin_id, instance_id, current_version, effective_version
            )

        target_class = (
            plugin_class if source_dir is None
            else self._load_plugin_class_for_version(plugin_id, desired, source_dir)
        )
        if target_class is not None:
            logger.info(f"插件实例 {key} 按绑定版本 {desired} 启动")
            if self._start_and_record(
                target_class, plugin_id, instance_id, desired, effective_version
            ):
                return True
        return self._recover_previous_version(
            plugin_class, plugin_id, instance_id, desired, effective_version, current_version
        )

    def __start_instance(self, plugin_class: Type[Any], plugin_id: str, instance_id: str) -> bool:
        """
        启动插件的单个实例
        :param plugin_class: 插件类
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: 实例是否成功进入运行态
        """
        key = instance_key(plugin_id, instance_id)
        try:
            # 构造、生效配置与建库期间执行的都是插件自己的代码（含插件自带的迁移
            # 脚本），其日志按实例归档，而不是落入插件兜底目录
            with bind_plugin_instance(plugin_id, instance_id):
                # 生成实例
                plugin_obj = self._instantiate_plugin(plugin_class, plugin_id, instance_id)
                extension = PluginExtension(plugin_obj, key)
                # 生效插件配置
                extension.initialize(self.get_plugin_config(key))
                # 按插件声明建立其数据库，未声明模型或迁移目录时不做任何事
                _plugin_database_ensure(plugin_id, instance_id)
            # 存储运行实例
            self._running_plugins[key] = plugin_obj
            logger.info(f"加载插件：{key} 版本：{plugin_obj.plugin_version}")
            # 同步插件声明的渠道能力
            self._sync_channel_capabilities(key)
            # 同步插件声明的存储类型与服务实例类型
            self._sync_plugin_service_types(key)
            # 同步插件声明的名称解析器
            self._sync_plugin_meta_parsers(key)
            # 同步插件声明的筛选规则与筛选规则组
            self._sync_plugin_filter_rules(key)
            # 同步插件声明的远程命令
            self._sync_plugin_commands(key)
            # 启用的实例才设置事件注册状态可用
            if extension.is_enabled():
                eventmanager.enable_event_handler(plugin_class, key)
            else:
                eventmanager.disable_event_handler(plugin_class, key)
            return True
        except Exception as err:
            logger.error(f"加载插件 {key} 出错：{str(err)} - {traceback.format_exc()}")
            self._running_plugins.pop(key, None)
            return False

    def _sync_family_event_state(self, plugin_id: str, plugin_class: Optional[Type[Any]]) -> None:
        """
        按插件全部实例的启用情况同步整类的事件注册状态

        整类停用会连带停掉全部实例，因此只有一个实例都没启用时才停用整类；
        单个实例的启停由实例级开关承担。
        :param plugin_id: 插件ID
        :param plugin_class: 插件类，取不到时不改变整类状态
        """
        if plugin_class is None:
            return
        enabled = any(
            self._instance_state(key, plugin)
            for key, plugin in self._matching_instances(plugin_id)
        )
        if enabled:
            eventmanager.enable_event_handler(plugin_class)
        else:
            eventmanager.disable_event_handler(plugin_class)

    @staticmethod
    def _instance_state(extension_id: str, plugin: Any) -> bool:
        """
        读取单个运行实例的启用状态
        :param extension_id: 实例键
        :param plugin: 插件运行实例
        :return: 实例已启用时为 True；实例不存在或读取出错时为 False
        """
        if plugin is None or not hasattr(plugin, "get_state"):
            return False
        try:
            return bool(plugin.get_state())
        except Exception as err:
            logger.error(f"获取插件 {extension_id} 状态出错：{str(err)}")
            return False

    def _resolve_instance_key(self, pid: str, instance_id: Optional[str] = None) -> Optional[str]:
        """
        定位单个运行实例的实例键
        :param pid: 插件ID或实例键
        :param instance_id: 实例标识，给出时与 pid 所属插件组合定位
        :return: 命中的实例键；未运行或按插件ID无法唯一确定时为 None
        """
        running = dict(self._running_plugins)
        if instance_id:
            key = instance_key(extension_id_of(pid), instance_id)
            return key if key in running else None
        if pid in running:
            return pid
        keys = [key for key in running if extension_id_of(key) == pid]
        return keys[0] if len(keys) == 1 else None

    def _resolve_call_target_key(self, pid: str) -> Optional[str]:
        """
        定位一次调用应当落到的运行实例键
        :param pid: 实例键，或插件ID（按该插件的默认调用目标裁决）
        :return: 命中的实例键；该插件没有实例在运行时为 None
        :raises LookupError: 该插件有实例在运行，但没有已启用的默认调用目标
        """
        if self._plugin_registry.instance(pid) is not None:
            return pid
        # 实例键精确未命中即该实例未运行，不按插件族另找一个顶替
        if pid != extension_id_of(pid):
            return None
        # 一个实例都没在跑属于「插件未加载」，与「选不出目标」是两回事
        if not self._plugin_registry.instance_keys(pid):
            return None
        return resolve_plugin_instance_key(pid)

    def _resolve_call_target(self, pid: str) -> Optional[Any]:
        """
        定位一次调用应当落到的运行实例
        :param pid: 实例键，或插件ID（按该插件的默认调用目标裁决）
        :return: 运行实例；该插件没有实例在运行时为 None
        :raises LookupError: 该插件有实例在运行，但没有已启用的默认调用目标
        """
        key = self._resolve_call_target_key(pid)
        return self._plugin_registry.instance(key) if key is not None else None

    def _matching_instances(self, pid: Optional[str]) -> List[Tuple[str, Any]]:
        """
        列出筛选条件命中的运行实例
        :param pid: 插件ID命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: `(实例键, 运行实例)` 列表
        """
        return [
            (key, plugin)
            for key, plugin in dict(self._running_plugins).items()
            if matches_extension(key, pid)
        ]

    def init_plugin(self, plugin_id: str, conf: dict, instance_id: Optional[str] = None):
        """
        初始化插件
        :param plugin_id: 插件ID或实例键
        :param conf: 插件配置
        :param instance_id: 实例标识，为空时按 plugin_id 定位实例
        """
        key = self._resolve_instance_key(plugin_id, instance_id)
        if not key:
            return
        plugin = self._running_plugins.get(key)
        if not plugin:
            return
        extension = PluginExtension(plugin, key)
        owner_plugin_id, owner_instance_id = split_instance_key(key)
        # 初始化插件，期间产生的日志按实例归档，而不是落入插件兜底目录
        with bind_plugin_instance(owner_plugin_id, owner_instance_id):
            extension.initialize(conf)
        # 检查实例状态并启用/禁用其事件处理器
        if extension.is_enabled():
            eventmanager.enable_event_handler(type(plugin), key)
        else:
            eventmanager.disable_event_handler(type(plugin), key)
        # 兄弟实例可能仍然启用，整类的事件注册状态按全族重新裁决
        self._sync_family_event_state(extension_id_of(key), type(plugin))
        # 配置变更可能启用或停用实例，重新同步渠道能力登记
        self._sync_channel_capabilities(key)
        # 配置变更同样可能影响存储与服务实例声明，重新同步服务类型登记
        self._sync_plugin_service_types(key)
        # 名称解析器声明同理，重新同步解析器登记
        self._sync_plugin_meta_parsers(key)
        # 筛选规则声明同理，重新同步规则登记
        self._sync_plugin_filter_rules(key)
        # 命令声明同理，重新同步命令登记
        self._sync_plugin_commands(key)
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

    def stop(self, pid: Optional[str] = None, instance_id: Optional[str] = None):
        """
        停止插件服务
        :param pid: 插件ID或实例键，为空停止所有插件
        :param instance_id: 实例标识，为空时停止 pid 命中的全部实例
        """
        # 停止插件
        if pid:
            logger.info(f"正在停止插件 {pid}...")
            keys = self._stop_targets(pid, instance_id)
            if not keys:
                # 指定插件可能在上次加载时已导入模块但初始化失败，此时不会进入运行态列表。
                # 仍需继续清理类缓存和 sys.modules，避免后续热重载反复复用旧模块。
                logger.debug(f"插件 {pid} 不存在或未加载")
        else:
            logger.info("正在停止所有插件...")
            keys = list(self._running_plugins)
        # 类对象在实例摘除后就取不到了，先留存用于停止后的事件停用
        classes = {
            extension_id_of(key): self._plugin_registry.plugin_class(key)
            for key in ([pid] if pid else []) + keys
        }
        for key in keys:
            plugin = self._running_plugins.get(key)
            if plugin is None:
                continue
            eventmanager.disable_event_handler(type(plugin), key)
            self.__stop_plugin(plugin)
            # 实例停止后撤销其渠道能力登记，不留残留
            self._revoke_channel_capabilities(key)
            # 实例停止后撤销其存储登记，不留残留
            self._revoke_plugin_storages(key)
            # 实例停止后撤销其服务实例登记，不留残留
            self._revoke_plugin_service_instances(key)
            # 实例停止后撤销其名称解析器登记，不留残留
            self._revoke_plugin_meta_parsers(key)
            # 实例停止后撤销其筛选规则登记，不留残留
            self._revoke_plugin_filter_rules(key)
            # 实例停止后撤销其命令登记，不留残留
            self._revoke_plugin_commands(key)
        # 清空对象
        if pid:
            single_instance = bool(instance_id) or pid != extension_id_of(pid)
            if single_instance:
                for key in keys:
                    self._plugin_registry.remove_instance(key)
            else:
                self._plugin_registry.remove(extension_id_of(pid))
            for plugin_id, plugin_class in classes.items():
                self._recycle_stopped_family(plugin_id, plugin_class)
                self._reelect_extension_registrations(plugin_id)
        else:
            # 清空
            self._plugin_registry.clear()
            for plugin_id, plugin_class in classes.items():
                if plugin_class is not None:
                    eventmanager.disable_event_handler(plugin_class)
                # 停止只释放数据库连接，不销毁库文件——销毁只在明确删除插件数据的路径触发
                _plugin_database_release(plugin_id)
            # 清除所有插件模块缓存
            self._clear_plugin_modules()
        self.clear_plugin_agent_tools_cache()
        logger.info("插件停止完成")

    def _stop_targets(self, pid: str, instance_id: Optional[str]) -> List[str]:
        """
        列出一次停止请求命中的运行实例键
        :param pid: 插件ID或实例键
        :param instance_id: 实例标识，为空时命中 pid 所指的全部实例
        :return: 实例键列表
        """
        running = dict(self._running_plugins)
        plugin_id = extension_id_of(pid)
        if instance_id:
            candidates = [instance_key(plugin_id, instance_id)]
        elif pid != plugin_id:
            candidates = [pid]
        else:
            candidates = [key for key in running if extension_id_of(key) == plugin_id]
        return [key for key in candidates if key in running]

    def _recycle_stopped_family(self, plugin_id: str, plugin_class: Optional[Type[Any]]) -> None:
        """
        在插件最后一个实例停止后回收其整族资源

        仍有兄弟实例在运行时整族资源必须原样保留：清除模块缓存会让运行中的实例
        与下次导入产生的类对象脱节，释放数据库连接会掐断兄弟实例正在用的会话。
        :param plugin_id: 插件ID
        :param plugin_class: 停止前留存的插件类，取不到时为 None
        """
        if self._plugin_registry.instance_keys(plugin_id):
            return
        if plugin_class is not None:
            eventmanager.disable_event_handler(plugin_class)
        # 停止只释放数据库连接，不销毁库文件——销毁只在明确删除插件数据的路径触发
        _plugin_database_release(plugin_id)
        # 清除插件模块缓存，包括所有子模块
        self._clear_plugin_modules(plugin_id)

    def _reelect_extension_registrations(self, plugin_id: str) -> None:
        """在实例停止后按存活实例重新裁决扩展级声明的登记归属

        存储标识、服务实例类型、筛选规则标识与命令词只登记一次，归属落在裁决胜出的那个实例。
        胜出实例停止后，仍在运行且声明同一标识的兄弟实例应当接手，否则该标识会随一个
        实例的停止整体消失——它描述的是「本宿主提供这个标识」，而宿主里还有实例提供它。
        最后一个实例停止后没有存活实例可裁决，登记在各自的撤销里已经清干净。
        :param plugin_id: 插件ID
        :return: 无返回值
        """
        survivors = self._plugin_registry.instance_keys(plugin_id)
        if not survivors:
            return
        self._sync_plugin_service_types(survivors[0])
        self._sync_plugin_filter_rules(survivors[0])
        self._sync_plugin_commands(survivors[0])

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

            # 定位本次要加载的版本目录，存量平铺布局在此顺带完成一次迁移
            source_dir = resolve_plugin_version_dir(plugin_dir)
            if source_dir is None:
                logger.debug(f"跳过插件目录：{plugin_dir.name}（没有可加载的版本目录）")
                continue

            # 检查__init__.py是否存在
            init_file = source_dir / "__init__.py"
            if not init_file.exists():
                logger.debug(f"跳过插件目录：{plugin_dir.name}（缺少__init__.py）")
                continue

            try:
                # 构建模块名
                module_name = plugin_module_name(plugin_dir, source_dir)
                logger.debug(f"正在导入插件模块：{module_name}")

                # 旧插件可能直接导入带宿主资源前置条件的第三方包。资源必须在
                # Python 执行插件模块顶层代码前就绪，否则导入副作用无法安全回滚。
                _legacy_plugin_import_preparer(
                    plugin_id=plugin_dir.name,
                    plugin_dir=source_dir,
                )

                _legacy_import_scanner(
                    plugin_id=plugin_dir.name,
                    plugin_dir=source_dir,
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

    def _monitor_enabled(self) -> bool:
        """判断当前是否允许运行插件文件修改监测。

        后台恢复期间源码与依赖仍在写入，监测开着会把半成品目录反复导入。
        :return: 允许运行监测时为 True
        """
        return (
            not self.is_plugin_settling()
            and bool(settings.DEV or settings.PLUGIN_AUTO_RELOAD)
        )

    def start_monitor(self):
        """按当前配置启动插件文件修改监测。"""
        if self._monitor_enabled():
            self.__start_monitor()

    def reload_monitor(self):
        """
        重新加载插件文件修改监测
        """
        # 先关闭已有监测；仍允许运行时再重新启动
        self.stop_monitor()
        if self._monitor_enabled():
            self.__start_monitor()

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
            self._process_watch_changes(changes)

    def _process_watch_changes(self, changes: Any) -> None:
        """
        处理一批插件目录文件变化事件
        :param changes: watchfiles 返回的 `(变化类型, 路径)` 集合
        :return: 无返回值
        """
        plugins_to_reload = set()
        local_plugins_to_sync = {}
        for _change_type, path_str in changes:
            event_path = Path(path_str)

            # 跳过 pycache 目录中的文件
            if "__pycache__" in event_path.parts:
                continue

            manifest_status = get_plugin_system().dependency_manifest_status(event_path)
            if manifest_status is not None:
                self._handle_dependency_manifest_change(event_path, active=manifest_status)
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

            # 解析插件ID与所属版本目录
            runtime_target = self._get_plugin_target_from_path(event_path)
            runtime_pid = runtime_target[0] if runtime_target else None
            # 安装或替换正在写入该插件目录时，半成品源码不得被抢先导入
            if runtime_pid and self.is_plugin_monitor_suppressed(runtime_pid):
                logger.debug(f"插件 {runtime_pid} 正在写入，跳过本批文件监控重载")
                continue
            local_candidate = self._get_local_plugin_candidate_from_path(event_path) if not runtime_pid else None
            if runtime_pid:
                last_sync_time = self._recent_local_sync.get(runtime_pid)
                if last_sync_time and time.time() - last_sync_time < 2:
                    continue
                # 运行目录变化只重载，不能反向触发本地同步。
                logger.debug(
                    f"插件 {runtime_pid} 版本目录 {runtime_target[1] or '存量布局'} 源码变化"
                )
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

    def _handle_dependency_manifest_change(self, event_path: Path, *, active: bool) -> None:
        """
        记录本地插件依赖清单变化，不在监控线程中隐式安装依赖
        :param event_path: 发生变化的依赖清单路径
        :param active: 该文件是否为插件当前生效的依赖清单
        :return: 无返回值
        """
        candidate = self._get_local_plugin_candidate_from_path(event_path)
        if not candidate:
            return
        if candidate.get("compatible") is False:
            logger.info(
                f"检测到本地插件 {candidate.get('id')} 依赖文件变化，"
                f"但跳过处理：{candidate.get('skip_reason')}"
            )
            return
        if not active:
            logger.debug(
                f"检测到本地插件 {candidate.get('id')} 非生效依赖文件变化：{event_path.name}"
            )
            return
        logger.warning(
            f"检测到本地插件 {candidate.get('id')} 依赖文件变化，请重新安装本地插件以安装依赖"
        )

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
                # 联邦构建产物随版本目录走，dist 路径相对版本目录解析
                if len(relative_parts) >= 2 and plugin_version_from_dir_name(relative_parts[1]):
                    plugin_dir = plugin_dir / relative_parts[1]
                pid = next(
                    (
                        extension_id_of(key)
                        for key in self._running_plugins
                        if extension_id_of(key).lower() == relative_parts[0].lower()
                    ),
                    None,
                )

            if not pid:
                return None
            # 联邦构建产物属于插件本身而非某个实例，取任一在运行的实例读取渲染模式
            plugin = next(
                (plugin for _key, plugin in self._matching_instances(pid)),
                None,
            )
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
    def _get_plugin_target_from_path(event_path: Path) -> Optional[Tuple[str, Optional[str]]]:
        """
        根据文件路径解析出插件的ID与所属版本目录。

        版本化布局下插件源码位于 app/plugins/<插件ID>/<版本目录>/，因此取相对
        路径的前两段；第二段不是版本目录时按存量平铺布局回落到插件目录本身。
        :param event_path: 被修改文件的 Path 对象。
        :return: (插件ID, 版本目录名) 元组，存量布局时版本目录名为 None；
            不是有效插件文件时返回 None。
        """
        try:
            event_path = event_path.resolve()
            plugins_root = settings.ROOT_PATH / "app" / "plugins"
            # 确保修改的文件在 plugins 目录下
            if not event_path.is_relative_to(plugins_root):
                return None

            try:
                relative_parts = event_path.relative_to(plugins_root).parts
                plugin_dir = plugins_root / relative_parts[0]
            except (ValueError, IndexError):
                return None

            version_dir_name = None
            source_dir = plugin_dir
            if len(relative_parts) >= 2 and plugin_version_from_dir_name(relative_parts[1]):
                version_dir_name = relative_parts[1]
                source_dir = plugin_dir / version_dir_name

            init_file = source_dir / "__init__.py"
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
                            return node.name, version_dir_name

            return None
        except Exception as e:
            logger.error(f"从路径解析插件ID时出错: {e}")
            return None

    @classmethod
    def _get_plugin_id_from_path(cls, event_path: Path) -> Optional[str]:
        """
        根据文件路径解析出插件的ID。
        :param event_path: 被修改文件的 Path 对象。
        :return: 插件ID字符串，如果不是有效插件文件则返回 None。
        """
        try:
            target = cls._get_plugin_target_from_path(event_path)
            return target[0] if target else None
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
        candidate_version = str(candidate.get("version") or "").strip()
        version_dir = None
        if candidate_version:
            try:
                version_dir = ensure_plugin_version_dir_available(dest_dir, candidate_version)
            except ValueError as err:
                logger.error(f"本地插件 {pid} 的版本号不可用于版本目录，跳过同步：{err}")
                return False
        try:
            if not get_plugin_system().package.sync_local(pid, source_dir, version_dir=version_dir):
                return False
            if version_dir:
                register_plugin_version(dest_dir, candidate_version, version_dir, source="local")
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

    @contextmanager
    def suppress_plugin_monitor(self, plugin_id: str):
        """在插件目录原子更新期间阻止文件监控抢先重载半成品。"""
        normalized_id = plugin_id.lower()
        with self._monitor_suppression_lock:
            self._suppressed_monitor_plugins[normalized_id] = (
                self._suppressed_monitor_plugins.get(normalized_id, 0) + 1
            )
        try:
            yield
        finally:
            with self._monitor_suppression_lock:
                count = self._suppressed_monitor_plugins.get(normalized_id, 0)
                if count <= 1:
                    self._suppressed_monitor_plugins.pop(normalized_id, None)
                else:
                    self._suppressed_monitor_plugins[normalized_id] = count - 1

    def is_plugin_monitor_suppressed(self, plugin_id: str) -> bool:
        """判断指定插件是否处于安装或替换写入阶段。"""
        with self._monitor_suppression_lock:
            return self._suppressed_monitor_plugins.get(plugin_id.lower(), 0) > 0

    def remove_plugin(self, plugin_id: str):
        """
        从内存中移除一个插件
        :param plugin_id: 插件ID
        """
        self.stop(plugin_id)

    def reload_plugin(self, plugin_id: str) -> PluginRuntimeStatus:
        """
        将一个插件重新加载到内存
        :param plugin_id: 插件ID
        :return: 重新加载后的插件运行状态
        """
        # 先移除插件实例
        self.stop(plugin_id)
        # 重新加载
        results = self.start(plugin_id)
        # 广播事件
        eventmanager.send_event(EventType.PluginReload, data={"plugin_id": plugin_id})
        return results.get(extension_id_of(plugin_id), PluginRuntimeStatus.LOAD_FAILED)

    @staticmethod
    def _clear_plugin_modules(plugin_id: Optional[str] = None, version_dir: Optional[str] = None):
        """
        清除插件及其所有子模块的缓存
        :param plugin_id: 插件ID，为空时清除全部插件模块，宿主包 ``app.plugins``
            本身保留：它是插件的宿主而非插件，把它一并逐出会让后续导入拿到另一个
            模块对象，命名空间包已扩展的搜索路径与模块级状态随之与旧对象脱节
        :param version_dir: 版本目录名，给定时只清该版本，兄弟版本的模块对象保留；
            插件命名空间包条目本身也保留，否则兄弟版本的父包会与之脱节
        """

        # 构建插件模块前缀
        if plugin_id and version_dir:
            plugin_module_prefix = f"app.plugins.{plugin_id.lower()}.{version_dir}"
        elif plugin_id:
            plugin_module_prefix = f"app.plugins.{plugin_id.lower()}"
        else:
            plugin_module_prefix = "app.plugins"

        # 收集需要删除的模块名（创建模块名列表的副本以避免迭代时修改字典）
        # 未指定插件时前缀即宿主包，只取其子孙模块，不含前缀自身
        include_prefix_itself = bool(plugin_id)
        modules_to_remove = []
        for module_name in list(sys.modules.keys()):
            if module_name == plugin_module_prefix and include_prefix_itself:
                modules_to_remove.append(module_name)
            elif module_name.startswith(plugin_module_prefix + "."):
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
            plugin_root = settings.ROOT_PATH / "app" / "plugins" / plugin.id.lower()
            # 仓库未给出版本号时按存量布局落盘，加载时再由布局迁移补上兜底版本目录
            declared_version = (plugin.plugin_version or "").strip()
            version_dir = None
            if declared_version:
                try:
                    version_dir = ensure_plugin_version_dir_available(plugin_root, declared_version)
                except ValueError as err:
                    logger.error(f"插件 {plugin.plugin_name} 的版本号不可用于版本目录，拒绝安装：{err}")
                    failed_plugins.append(plugin.id)
                    return
            # 后台自动更新保留旧版本备份，下载或依赖安装失败时由安装器还原
            state, msg = get_plugin_system().package.install(
                plugin_id=plugin.id,
                repo_url=plugin.repo_url,
                force_install=False,
                version_dir=version_dir,
            )
            elapsed_time = time.time() - start_time
            if state:
                if version_dir:
                    register_plugin_version(plugin_root, declared_version, version_dir, source="market")
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
        :return: 本轮检查到的缺失依赖项
        """
        return PluginManager.install_plugin_missing_dependencies_with_status().missing

    @staticmethod
    def install_plugin_missing_dependencies_with_status() -> PluginDependencyInstallResult:
        """
        安装插件中缺失或不兼容的依赖项，并给出安装器的明确结果
        :return: 缺失依赖项与本次安装是否全部成功
        """
        dependency_installer = get_plugin_system().dependency
        # 第一步：获取需要安装的依赖项列表
        missing_dependencies = dependency_installer.find_missing()
        if not missing_dependencies:
            return PluginDependencyInstallResult(missing=[], success=True)
        logger.debug(f"检测到缺失的依赖项: {missing_dependencies}")
        logger.info(f"开始安装缺失的依赖项，共 {len(missing_dependencies)} 个...")
        # 第二步：安装依赖项并返回结果
        total_start_time = time.time()
        success, _message = dependency_installer.install(missing_dependencies)
        total_elapsed_time = time.time() - total_start_time
        if success:
            logger.info(f"已完成 {len(missing_dependencies)} 个依赖项安装，总耗时：{total_elapsed_time:.2f} 秒")
        else:
            logger.warning(f"存在缺失依赖项安装失败，请尝试手动安装，总耗时：{total_elapsed_time:.2f} 秒")
        return PluginDependencyInstallResult(
            missing=missing_dependencies,
            success=bool(success),
        )

    @staticmethod
    def classify_plugins() -> PluginDependencyClassification:
        """
        按源码和依赖是否就绪划分已安装插件
        :return: 就绪、等待依赖和等待源码三类插件ID
        """
        ready, missing_dependencies, missing_source = (
            get_plugin_system().dependency.classify_plugins()
        )
        return PluginDependencyClassification(
            ready=tuple(ready),
            missing_dependencies=tuple(missing_dependencies),
            missing_source=tuple(missing_source),
        )

    def apply_plugin_dependency_classification(
        self,
        classification: PluginDependencyClassification,
    ) -> None:
        """
        把源码和依赖分类写入运行状态，已激活插件保持当前结果
        :param classification: 本轮源码与依赖分类
        :return: 无返回值
        """
        running_ids = set(self._plugin_registry.running_plugin_ids())
        for plugin_id in classification.missing_source:
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.SOURCE_MISSING,
            )
        for plugin_id in classification.missing_dependencies:
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.DEPENDENCY_PENDING,
            )
        for plugin_id in classification.ready:
            current_status = self._plugin_registry.runtime_status(plugin_id)
            # 已在运行且不是依赖刚恢复的插件保持现状，避免把 active 改回 ready
            if (
                plugin_id in running_ids
                and current_status is not PluginRuntimeStatus.DEPENDENCY_PENDING
            ):
                continue
            self._plugin_registry.set_runtime_status(
                plugin_id,
                PluginRuntimeStatus.READY,
            )

    def set_plugin_settling(self, settling: bool) -> None:
        """
        更新启动后的插件恢复任务状态
        :param settling: 后台源码与依赖恢复任务是否仍在执行
        :return: 无返回值
        """
        self._plugin_registry.set_settling(settling)

    def get_plugin_runtime_statuses(self) -> Dict[str, PluginRuntimeStatus]:
        """
        返回插件运行状态快照
        :return: 插件ID到运行状态的映射
        """
        return self._plugin_registry.runtime_status_snapshot()

    def get_plugin_runtime_generation(self) -> int:
        """
        返回插件状态变化代次
        :return: 自进程启动以来的状态变化次数
        """
        return self._plugin_registry.generation

    def is_plugin_settling(self) -> bool:
        """
        返回插件源码和依赖是否仍在后台恢复
        :return: 后台恢复任务仍在执行时为 True
        """
        return self._plugin_registry.settling

    def get_plugin_config(self, pid: str, instance_id: Optional[str] = None) -> dict:
        """
        获取插件配置
        :param pid: 插件ID或实例键
        :param instance_id: 实例标识，为空时取 pid 所指实例，pid 为插件ID时取默认实例
        """
        plugin_id, resolved_instance_id = split_instance_key(pid)
        if instance_id:
            resolved_instance_id = normalize_instance_id(instance_id)
        if not self._plugins.get(plugin_id):
            return {}
        conf = get_plugin_storage().read_config(plugin_id, resolved_instance_id)
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
        get_plugin_storage().write_config(pid, conf)
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
        await get_plugin_storage().async_write_config(pid, conf)
        return True

    def delete_plugin_config(self, pid: str, force: bool = False) -> bool:
        """
        删除插件配置
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化配置
        """
        if not force and not self._plugins.get(pid):
            return False
        return get_plugin_storage().delete_config(pid)

    def delete_plugin_data(self, pid: str, force: bool = False) -> bool:
        """
        删除插件数据
        :param pid: 插件ID
        :param force: 插件停止后仍允许按插件 ID 删除持久化数据
        """
        if not force and not self._plugins.get(pid):
            return False
        get_plugin_storage().delete_data(pid)
        # 删除插件数据时才销毁其数据库文件，这是不可逆操作，全部实例一并销毁
        for target in self._plugin_instance_ids(pid):
            _plugin_database_destroy(pid, target)
        return True

    def _instance_info(self, plugin_id: str, instance_id: str) -> Dict[str, Any]:
        """
        组装单个实例的信息
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: 含实例标识、实例键、是否在运行态、启用状态的字典
        """
        key = instance_key(plugin_id, instance_id)
        plugin = self._running_plugins.get(key)
        return {
            "instance_id": instance_id,
            "instance_key": key,
            "running": plugin is not None,
            "state": self._instance_state(key, plugin) if plugin is not None else False,
        }

    def list_plugin_instances(self, plugin_id: str) -> List[Dict[str, Any]]:
        """
        列出插件的全部实例及其运行状态
        :param plugin_id: 插件ID
        :return: 实例信息列表，按实例标识升序排列
        :raises LookupError: 插件不存在
        """
        if plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        configured_ids = set(get_plugin_storage().list_instances(plugin_id))
        running_ids = {
            split_instance_key(key)[1]
            for key in self._plugin_registry.instance_keys(plugin_id)
        }
        all_ids = configured_ids | running_ids or {DEFAULT_INSTANCE_ID}
        return [self._instance_info(plugin_id, instance_id) for instance_id in sorted(all_ids)]

    def plugin_version_binding(self, plugin_id: str, instance_id: str) -> Dict[str, Any]:
        """
        组装单个实例的版本绑定信息

        期望版本按跟随规则解析，跟随插件当前版本时回落到当前版本目录对应的版本号，
        与已生效版本不一致即表示待切换。
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :return: 含实例标识、实例键、已生效版本、跟随开关、期望版本与运行状态的字典
        """
        key = instance_key(plugin_id, instance_id)
        version, follow = self._instance_version_binding(plugin_id, instance_id)
        target_version = self._desired_instance_version(plugin_id, instance_id) or getattr(
            self._plugins.get(plugin_id), "plugin_version", None
        )
        return {
            "instance_id": instance_id,
            "instance_key": key,
            "plugin_version": version,
            "follow_default_version": follow,
            "target_version": target_version,
            "running": key in self._running_plugins,
        }

    def list_plugin_versions(self, plugin_id: str) -> Dict[str, Any]:
        """
        列出插件已装版本与各实例的版本绑定情况
        :param plugin_id: 插件ID
        :return: 含已装版本列表与各实例绑定信息的字典
        :raises LookupError: 插件不存在
        """
        if plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        plugin_root = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        manifest = read_plugin_versions_manifest(plugin_root)
        current = manifest.get("current")
        current_version = current if isinstance(current, str) and current else None
        registered = {
            entry.get("version"): entry
            for entry in (manifest.get("versions") or [])
            if isinstance(entry, dict)
        }
        installed_versions = [
            {
                "version": version,
                "directory": path.name,
                "installed_at": (registered.get(version) or {}).get("installed_at"),
                "source": (registered.get(version) or {}).get("source"),
                "is_current": version == current_version,
            }
            for version, path in sorted(plugin_version_dirs(plugin_root).items())
        ]
        return {
            "plugin_id": plugin_id,
            "current_version": current_version,
            "installed_versions": installed_versions,
            "instances": [
                self.plugin_version_binding(plugin_id, info["instance_id"])
                for info in self.list_plugin_instances(plugin_id)
            ],
        }

    def _ensure_version_bindable(self, plugin_id: str, instance_id: str, version: str) -> None:
        """
        校验目标版本已安装，且该插件允许把不同实例绑到不同版本
        :param plugin_id: 插件ID
        :param instance_id: 待改绑的实例标识
        :param version: 目标版本号
        :raises ValueError: 目标版本未安装，或插件写法不支持多版本并存
        """
        plugin_root = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        installed = plugin_version_dirs(plugin_root)
        if version not in installed:
            raise ValueError(f"插件 {plugin_id} 未安装版本 {version}")
        current_version = getattr(self._plugins.get(plugin_id), "plugin_version", None)
        sibling_versions = {
            self._desired_instance_version(plugin_id, other) or current_version
            for other in self._plugin_instance_ids(plugin_id)
            if other != instance_id
        }
        sibling_versions.discard(None)
        if not sibling_versions - {version}:
            # 全部兄弟实例都会落在同一个版本上，不构成多版本并存
            return
        blockers = _plugin_multi_version_probe(plugin_id.lower(), list(installed.values()))
        if blockers:
            raise ValueError(
                f"插件 {plugin_id} 的写法不支持多版本并存，拒绝按版本分别绑定实例："
                + "；".join(blockers)
            )

    def set_plugin_instance_version(
        self,
        plugin_id: str,
        instance_id: str,
        *,
        version: Optional[str] = None,
        follow_default_version: bool = True,
    ) -> Dict[str, Any]:
        """
        设置实例绑定的插件版本与跟随开关，并立即完成一次停止再启动

        已生效版本一列只登记启动成功的版本，因此目标版本不预先落库：切换成功由启动
        路径写入新版本，切换失败则保持旧值并以旧版本重新启动。
        :param plugin_id: 插件ID
        :param instance_id: 实例标识
        :param version: 目标版本号，跟随默认实例时忽略
        :param follow_default_version: 是否跟随默认实例的版本
        :return: 该实例最新的版本绑定信息
        :raises LookupError: 插件不存在，或该实例未登记
        :raises ValueError: 实例标识非法、未指定目标版本、目标版本未安装，
            或该插件写法不支持多版本并存
        """
        plugin_class = self._plugins.get(plugin_id)
        if plugin_class is None:
            raise LookupError(f"插件 {plugin_id} 不存在")
        normalized_instance_id = normalize_instance_id(instance_id)
        known_ids = {info["instance_id"] for info in self.list_plugin_instances(plugin_id)}
        if normalized_instance_id not in known_ids:
            raise LookupError(f"插件实例 {plugin_id}@{normalized_instance_id} 不存在")

        if follow_default_version:
            # 跟随时的目标版本由启动路径按写入后的跟随开关重新解析
            target_version = None
        else:
            target_version = (version or "").strip() or None
            if not target_version:
                raise ValueError("未跟随默认实例时必须指定目标版本")
            self._ensure_version_bindable(plugin_id, normalized_instance_id, target_version)

        _plugin_instance_follow_write(
            plugin_id, normalized_instance_id, bool(follow_default_version)
        )
        # 停止再启动是干净的生命周期切换，不做热替换
        self.stop(plugin_id, normalized_instance_id)
        self._start_instance_with_version(
            plugin_class, plugin_id, normalized_instance_id, target_version
        )
        self._sync_family_event_state(plugin_id, plugin_class)
        self.clear_plugin_agent_tools_cache()
        if normalized_instance_id == DEFAULT_INSTANCE_ID:
            self.restart_version_following_instances(plugin_id)
        return self.plugin_version_binding(plugin_id, normalized_instance_id)

    def restart_version_following_instances(self, plugin_id: str) -> List[str]:
        """
        把跟随默认版本的实例逐个停止再启动，切到默认实例当前已生效的版本

        热替换等于在运行期换掉一个已注册事件、已起定时任务、可能有在途请求的实例，
        因此这里走完整的实例级停止再启动；单个实例切换失败不波及兄弟实例。
        :param plugin_id: 插件ID
        :return: 实际触发切换的实例标识列表
        :raises LookupError: 插件不存在
        """
        plugin_class = self._plugins.get(plugin_id)
        if plugin_class is None:
            raise LookupError(f"插件 {plugin_id} 不存在")
        target_version, _ = self._instance_version_binding(plugin_id, DEFAULT_INSTANCE_ID)
        if not target_version:
            logger.debug(f"插件 {plugin_id} 的默认实例尚无已生效版本，跟随实例无需切换")
            return []
        switched: List[str] = []
        for target in self._plugin_instance_ids(plugin_id):
            if target == DEFAULT_INSTANCE_ID:
                continue
            version, follow = self._instance_version_binding(plugin_id, target)
            if not follow or version == target_version:
                continue
            logger.info(
                f"插件实例 {instance_key(plugin_id, target)} 跟随默认实例切换到版本 "
                f"{target_version}"
            )
            self.stop(plugin_id, target)
            self._start_instance_with_version(plugin_class, plugin_id, target, target_version)
            switched.append(target)
        if switched:
            self._sync_family_event_state(plugin_id, plugin_class)
            self.clear_plugin_agent_tools_cache()
        return switched

    def _plugin_referenced_versions(self, plugin_id: str) -> Set[str]:
        """
        收集插件全部实例当前占用的版本号，供版本回收判断某版本是否仍在用

        覆盖两类引用：实例自己那一行的已生效版本，以及按跟随开关解析出的期望
        版本。跟随默认实例的实例在切换前那一行仍记着旧版本——它此刻仍在用那个
        旧版本运行，因此已生效版本必须无条件纳入；期望版本则覆盖它下一次启动
        就会切到的版本。两者但凡漏一个，回收都可能删掉正在用或即将用的版本。

        实例清单与版本绑定在此处直读、读取出错直接上抛，不走会把出错吞成空值的
        那两个读取口：读不到就凑不出完整的引用集合，此时按空集合回收会删掉仍在
        用的版本且无从恢复，让调用方跳过本次回收才是安全的失效方向。
        :param plugin_id: 插件ID
        :return: 被引用的版本号集合
        :raises Exception: 实例清单或版本绑定读取失败
        """
        bindings = {
            target: (_plugin_instance_version_read(plugin_id, target) or (None, True))
            for target in self._read_plugin_instance_ids(plugin_id)
        }
        default_version = (bindings.get(DEFAULT_INSTANCE_ID) or (None, True))[0] or None
        referenced: Set[str] = set()
        for target, (effective_version, follow) in bindings.items():
            effective_version = effective_version or None
            if effective_version:
                referenced.add(effective_version)
            if target == DEFAULT_INSTANCE_ID:
                # 默认实例跟随的是插件当前安装版本，当前版本另有保留判据兜住
                continue
            desired_version = (default_version or effective_version) if follow else effective_version
            if desired_version:
                referenced.add(desired_version)
        return referenced

    def recycle_plugin_versions(self, plugin_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        回收插件源码目录下没有实例引用、不在保留窗口内的旧版本目录

        判据的三个取值都读实测的运行态与配置：已生效版本与期望版本来自
        ``_plugin_referenced_versions``，当前安装版本与保留窗口来自版本元信息，
        具体判定落在 ``layout.recycle_plugin_version_directories``。只有插件加载
        之后才能读到全部实例的版本绑定，因此调用方须固定放在启动流程里、插件
        加载完成之后；不要放在安装流程里删，安装期用户可能正打算回退到旧版本。
        单个插件回收出错不影响其余插件，也不向上抛出，失败不阻断启动。
        :param plugin_id: 插件ID，为空处理全部已加载插件；显式指定但插件不存在时报错
        :return: 插件ID到本次回收结果（含 removed 与 kept）的映射
        :raises LookupError: 显式指定的插件不存在
        """
        if plugin_id is not None and plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        targets = [plugin_id] if plugin_id else list(self._plugins)
        results: Dict[str, Dict[str, Any]] = {}
        for target in targets:
            try:
                plugin_root = settings.ROOT_PATH / "app" / "plugins" / target.lower()
                referenced = self._plugin_referenced_versions(target)
                outcome = recycle_plugin_version_directories(plugin_root, referenced)
            except Exception as err:
                logger.error(f"插件 {target} 版本回收出错：{str(err)}")
                continue
            results[target] = outcome
            if outcome["removed"]:
                logger.info(
                    f"插件 {target} 版本回收：删除版本 {outcome['removed']}；"
                    f"保留版本及理由 {outcome['kept']}"
                )
            else:
                logger.debug(f"插件 {target} 版本回收：无可删除版本，保留 {outcome['kept']}")
        return results

    def create_plugin_instance(
        self, plugin_id: str, instance_id: str, config: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        创建插件的新实例：写入初始配置并拉起

        实例标识先后经过分隔符校验与单层目录名安全校验，全部通过才会触达配置表；
        该插件此前一条实例配置记录都没有时，先把默认实例固化成一行，否则默认实例
        会因为没有记录在下次启动时缺席。
        :param plugin_id: 插件ID
        :param instance_id: 新实例标识
        :param config: 实例初始配置，为空时使用空字典
        :return: 新实例信息
        :raises LookupError: 插件不存在
        :raises ValueError: 实例标识非法，或该实例已存在
        """
        if plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        normalized_instance_id = normalize_instance_id(instance_id)
        if normalized_instance_id == DEFAULT_INSTANCE_ID:
            raise ValueError("默认实例已存在，无需创建")
        # 路径分段校验先于任何文件或数据库写入，拒绝目录穿越等非法实例标识
        ensure_path_segment(normalized_instance_id, subject="插件实例ID")

        existing_ids = set(get_plugin_storage().list_instances(plugin_id))
        already_running = bool(self._resolve_instance_key(plugin_id, normalized_instance_id))
        if normalized_instance_id in existing_ids or already_running:
            raise ValueError(f"插件实例 {plugin_id}@{normalized_instance_id} 已存在")
        if not existing_ids:
            # 此前没有任何实例配置记录，先固化默认实例
            _plugin_instance_config_upsert(
                plugin_id,
                DEFAULT_INSTANCE_ID,
                self.get_plugin_config(plugin_id, DEFAULT_INSTANCE_ID) or {},
            )
        _plugin_instance_config_upsert(plugin_id, normalized_instance_id, config or {})
        self.start_instance(plugin_id, normalized_instance_id)
        return self._instance_info(plugin_id, normalized_instance_id)

    @staticmethod
    def _remove_instance_directory(plugin_id: str, instance_id: str) -> None:
        """
        删除插件实例的持久化目录，不可逆操作

        目录路径取自 ``plugin_instance_path`` 已校验的结果，经 ``resolve()`` 后
        三重校验（位于插件持久化根目录之内、不等于该根目录本身、目录名与实例标识
        一致）全部通过才执行删除。
        :param plugin_id: 插件ID
        :param instance_id: 实例标识，调用方需确保不是默认实例
        """
        from app.runtime.extensions.lifecycle.paths import plugin_instance_path
        instance_dir = plugin_instance_path(plugin_id, instance_id, "data").parent
        plugin_root = instance_dir.parent
        resolved_instance_dir = instance_dir.resolve()
        resolved_plugin_root = plugin_root.resolve()
        checks_passed = (
            resolved_instance_dir.is_relative_to(resolved_plugin_root)
            and resolved_instance_dir != resolved_plugin_root
            and resolved_instance_dir.name == instance_id
        )
        if not checks_passed:
            logger.error(f"插件实例目录校验未通过，跳过删除：{resolved_instance_dir}")
            return
        if resolved_instance_dir.exists():
            shutil.rmtree(resolved_instance_dir, ignore_errors=True)

    def delete_plugin_instance(self, plugin_id: str, instance_id: str) -> None:
        """
        删除插件的单个实例

        依次停止其运行态、删除配置行与数据行、销毁其自管理数据库（含 SQLite 预写
        日志与共享内存边车文件）、删除其持久化目录；兄弟实例不受影响。
        :param plugin_id: 插件ID
        :param instance_id: 待删除的实例标识
        :raises LookupError: 插件不存在，或该实例未登记
        :raises ValueError: 默认实例不可删除
        """
        if plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        normalized_instance_id = normalize_instance_id(instance_id)
        if normalized_instance_id == DEFAULT_INSTANCE_ID:
            raise ValueError("默认实例不可删除")

        existing_ids = set(get_plugin_storage().list_instances(plugin_id))
        still_running = bool(self._resolve_instance_key(plugin_id, normalized_instance_id))
        if normalized_instance_id not in existing_ids and not still_running:
            raise LookupError(f"插件实例 {plugin_id}@{normalized_instance_id} 不存在")

        # 停止运行态：撤销事件与渠道能力登记，兄弟实例不受影响
        self.stop(plugin_id, normalized_instance_id)
        # 删除配置与业务数据行
        _plugin_instance_config_delete(plugin_id, normalized_instance_id)
        _plugin_instance_data_delete(plugin_id, normalized_instance_id)
        # 销毁自管理数据库，不可逆操作
        _plugin_database_destroy(plugin_id, normalized_instance_id)
        # 删除持久化数据目录，三重校验后执行，不可逆操作
        self._remove_instance_directory(plugin_id, normalized_instance_id)

    def uninstall_plugin(self, plugin_id: str) -> None:
        """
        卸载插件

        停止其运行态、回收注册表中的类，并清除其模块缓存，避免同名插件重装后
        复用旧模块。配置、业务数据、持久化数据目录与源码目录均不删除——用户
        重新安装同一插件时可以读回原有配置，这是本方法刻意保留的软卸载语义。
        已安装清单登记、动态 API/定时任务/命令的注销、文件夹归属清理由调用方
        负责，不属于插件包和运行态本身的清理范围。
        :param plugin_id: 插件ID
        :raises LookupError: 插件不存在
        """
        if plugin_id not in self._plugins:
            raise LookupError(f"插件 {plugin_id} 不存在")
        # 停止运行态：撤销事件与渠道能力登记
        self.remove_plugin(plugin_id)
        # 确保内存中不再持有该插件类，并清除模块缓存，避免同名插件重装后复用旧模块
        self._plugins.pop(plugin_id, None)
        self._clear_plugin_modules(plugin_id)

    def get_plugin_state(self, pid: str) -> bool:
        """
        获取插件状态
        :param pid: 插件ID或实例键，插件ID时任一实例启用即为启用
        """
        return any(
            self._instance_state(key, plugin)
            for key, plugin in self._matching_instances(pid)
        )

    def _plugin_projection(self) -> PluginProjection:
        """构造绑定当前运行态插件注册表的能力投影服务。"""
        return PluginProjection(
            self._plugin_registry.running,
            logger,
            self.get_plugin_remote_entry,
        )

    @staticmethod
    def _log_channel_capability_handovers(
        plugin_id: str, handovers: Iterable[Any]
    ) -> None:
        """就一次登记引起的渠道能力归属变更各打一条日志。

        :param plugin_id: 本次登记方的插件实例键
        :param handovers: 归属变更条目
        :return: 无返回值
        """
        for handover in handovers:
            if handover.current_owner == plugin_id and handover.previous_owner:
                logger.warning(
                    f"插件[{plugin_id}]登记的渠道 {handover.identity} 能力覆盖了"
                    f"[{handover.previous_owner}]已登记的同一渠道：渠道标识指称同一个"
                    f"外部渠道，按后登记生效处置；被覆盖的那一份仍在表内，本插件停用后"
                    f"由它重新接手"
                )
            elif handover.previous_owner == plugin_id and handover.current_owner:
                logger.info(
                    f"插件[{plugin_id}]撤销渠道 {handover.identity} 能力登记，"
                    f"该渠道交回此前被它覆盖的[{handover.current_owner}]"
                )

    def _sync_channel_capabilities(self, plugin_id: str) -> None:
        """按插件当前声明重建其在渠道能力管理器中的登记。

        :param plugin_id: 插件 ID
        :return: 无返回值
        """
        try:
            declared = self._plugin_projection().channel_capabilities(plugin_id)
            handovers = ChannelCapabilityManager.register_extension_capabilities(
                plugin_id, declared.get(plugin_id, [])
            )
            self._log_channel_capability_handovers(plugin_id, handovers)
        except Exception as error:
            logger.error(f"同步插件 {plugin_id} 渠道能力登记出错：{str(error)}")

    @classmethod
    def _revoke_channel_capabilities(cls, plugin_id: str) -> None:
        """撤销插件在渠道能力管理器中的登记。

        插件停止后其启用状态声明不再可信，须直接清空登记，不能依赖
        重新查询插件当前声明的同步路径。

        :param plugin_id: 插件 ID
        :return: 无返回值
        """
        try:
            handovers = ChannelCapabilityManager.register_extension_capabilities(
                plugin_id, []
            )
            cls._log_channel_capability_handovers(plugin_id, handovers)
        except Exception as error:
            logger.error(f"撤销插件 {plugin_id} 渠道能力登记出错：{str(error)}")

    def _sync_plugin_service_types(self, key: str) -> None:
        """按插件当前全部运行实例的声明重建其提供的服务类型登记。

        类型标识是扩展级事实，同一插件的多个实例声明同一标识只登记一次，胜出方
        由投影按稳定规则裁决，因此同步粒度是整个插件而不是单个实例——只重建触发方
        一个实例的话，兄弟实例的落选与接手都无从体现。

        先回收各实例此前登记过的类型，避免声明缩减后残留旧登记；再按当前声明逐条
        登记，单条声明的注册失败不影响其余声明的登记。存储类型两张表各有一条登记，
        两处的回收与重建都在这一轮里做完：分两轮做的话，后跑的一方按登记方回收时会
        把先跑的一方刚建好的登记扫掉。

        :param key: 触发本次同步的实例键
        :return: 无返回值
        """
        try:
            projection = self._plugin_projection()
            instances = projection.provided_service_instances(extension_id_of(key))
            for owner in self._extension_registration_owners(key):
                storage_backend_registry.unregister_owner(owner)
                service_instance_registry.unregister_owner(owner)
                plugin = self._running_plugins.get(owner)
                self._register_declared_service_instances(
                    projection, owner, plugin, instances.get(owner, [])
                )
        except Exception as error:
            logger.error(f"同步插件实例 {key} 服务类型登记出错：{str(error)}")

    def _register_declared_service_instances(
        self, projection: PluginProjection, owner: str, plugin: Optional[Any],
        declared: List[Any]
    ) -> None:
        """登记一个插件实例声明的全部服务实例类型。

        :param projection: 已绑定当前运行态插件注册表的能力投影服务
        :param owner: 实例键
        :param plugin: 运行态插件实例
        :param declared: 该实例已通过登记契约校验的服务实例声明
        :return: 无返回值
        """
        for item in declared:
            try:
                capability, service_type, name = declaration_service_instance_identity(item)
                impl, factory = declaration_service_instance_constructor(item)
                multi_instance = declaration_service_instance_multi_instance(item)
                config_component = self._resolve_service_instance_config_component(
                    projection, owner, plugin, item
                )
                if capability == STORAGE_CAPABILITY:
                    impl, factory = self._register_declared_storage_backend(
                        owner, service_type, impl, factory,
                        declaration_config_form(item), config_component, multi_instance
                    )
                service_instance_registry.register(
                    capability=capability,
                    service_type=service_type,
                    name=name,
                    owner=owner,
                    icon=declaration_service_instance_icon(item),
                    impl=impl,
                    factory=factory,
                    multi_instance=multi_instance,
                    distribution=ExtensionDistribution.MARKET,
                    config_form=declaration_config_form(item),
                    config_component=config_component,
                    config_schema=declaration_config_schema(item),
                )
            except Exception as error:
                logger.error(f"登记插件实例 {owner} 的服务实例声明出错：{str(error)}")

    @staticmethod
    def _declared_storage_instance_specs(
        storage_id: str, multi_instance: bool
    ) -> Tuple[Tuple[Optional[str], bool], ...]:
        """列出某个存储类型当前应当占据的实例位。

        实例位来自该类型的配置：配了几份就有几个具名实例位，实例名即配置名，承接
        裸令牌的那一位由配置里的兼容指针裁出。一份配置都没有的类型仍留一个未具名
        实例位，未配置的存储因此照样可以浏览与登录——与内建存储模块同一口径。

        读取或裁决失败按未配置处理：配置来源不可用、或单实例类型裁决不出唯一目标，
        都不该让整个存储类型从注册表消失。

        :param storage_id: 存储标识
        :param multi_instance: 该类型能否接受多份配置
        :return: (实例名, 是否承接裸令牌) 二元组序列，实例名为 None 表示裸令牌位
        """
        try:
            selected = select_instance_configs(
                service_capability_configs(STORAGE_CAPABILITY),
                storage_id,
                capability=STORAGE_CAPABILITY,
                multi_instance=multi_instance,
            )
        except Exception as error:
            logger.error(f"读取存储 {storage_id} 的实例配置失败，按未配置处理：{str(error)}")
            selected = {}
        specs = tuple(
            (
                (getattr(conf, "name", None) or "").strip() or None,
                bool(getattr(conf, "bare_token_target", False)),
            )
            for conf in selected.values()
        )
        return specs or ((None, False),)

    @staticmethod
    def _register_declared_storage_backend(
        owner: str, storage_id: str, impl: Any, factory: Optional[Any],
        config_form: Optional[Any], config_component: Optional[Dict[str, Any]],
        multi_instance: bool = True
    ) -> tuple:
        """把存储类型的后端按实例逐条登进存储后端注册表，并补上类型目录要用的构造工厂。

        存储类型落两张表：存储后端注册表按令牌回答「``u115@work`` 指的实体是谁」，
        类型目录按类型回答「谁提供、能配几份、配置什么形状、界面长什么样」。粒度不同，
        后者也回答不了按实例寻址，因此不是同一份登记的两个副本。

        登记按实例位逐条进行：只登记一条裸令牌位的话，具名令牌在整理编排里一律解析
        不到实例，该类型就只有一个账号能用。单个实例位登记失败只跳过它自己，同类型
        其余实例位照常登记。

        构造一律走工厂：声明没给工厂时用宿主默认那一个，按实例归属交付后端、配置由
        后端自己按令牌懒读；给了就用声明自带的。类型目录因此不再持有 ``impl``——存储的
        ``impl`` 不是「按关键字展开配置构造」的那种实现类，留着会让通用构造路径按错误的
        协议调用它。

        :param owner: 实例键
        :param storage_id: 存储标识，即声明的类型标识
        :param impl: 声明携带的存储后端类
        :param factory: 声明携带的实例工厂，为 None 表示用宿主默认工厂
        :param config_form: 该类型的专属配置界面（vuetify 模式）
        :param config_component: 该类型的已解析 vue 模式配置组件
        :param multi_instance: 该类型能否接受多份配置
        :return: (类型目录用的实现类, 类型目录用的实例工厂) 二元组
        """
        specs = PluginManager._declared_storage_instance_specs(storage_id, multi_instance)
        for instance, bare_token_target in specs:
            try:
                storage_backend_registry.register(
                    impl,
                    distribution=ExtensionDistribution.MARKET,
                    owner=owner,
                    storage_id=storage_id,
                    config_form=config_form,
                    config_component=config_component,
                    instance=instance,
                    bare_token_target=bare_token_target,
                )
            except Exception as error:
                logger.error(
                    f"登记存储 {storage_id} 的实例 {instance or '裸令牌'} 出错，已跳过：{str(error)}"
                )
        return None, factory or storage_instance_factory(impl)

    def resync_declared_service_types(self) -> None:
        """按各运行插件当前的声明重建其提供的服务类型登记。

        存储登记按实例位展开，实例位来自该存储类型的当前配置：增删一份配置即多出或
        少掉一个可寻址的实例位，不重建则新配的账号按令牌取不到、已删的账号仍取得到。
        重建以扩展为单位，每个扩展取其一个运行实例键触发，兄弟实例在同一轮内一并重建。

        :return: 无返回值
        """
        seen: Set[str] = set()
        for key in list(self._running_plugins):
            extension_id = extension_id_of(key)
            if extension_id in seen:
                continue
            seen.add(extension_id)
            self._sync_plugin_service_types(key)

    def _extension_registration_owners(self, key: str) -> List[str]:
        """列出一次扩展级同步需要重建登记的实例键。

        取该插件当前全部运行实例，并保证触发方在列——触发方可能正处在停止途中、
        已不在运行态表里，它此前登记的条目仍须被回收。

        :param key: 触发本次同步的实例键
        :return: 实例键列表
        """
        owners = list(self._plugin_registry.instance_keys(extension_id_of(key)))
        if key not in owners:
            owners.append(key)
        return owners

    @staticmethod
    def _resolve_service_instance_config_component(
        projection: PluginProjection, key: str, plugin: Optional[Any], item: Any
    ) -> Optional[Dict[str, Any]]:
        """把服务实例声明携带的 vue 模式组件名解析为完整的联邦远程描述。

        声明未带组件名、或实例键取不到运行态插件实例时不解析，登记项的
        ``config_component`` 保持为空，等价于该服务类型没有专属界面。

        :param projection: 已绑定当前运行态插件注册表的能力投影服务
        :param key: 实例键
        :param plugin: 运行态插件实例
        :param item: 已通过登记契约校验的服务实例声明
        :return: 含 component 与 remote 的字典；无需解析时为 None
        """
        component = declaration_config_component(item)
        if not component or plugin is None:
            return None
        return projection.service_instance_component_descriptor(key, plugin, component)

    @staticmethod
    def _revoke_plugin_service_instances(key: str) -> None:
        """撤销插件实例登记的服务实例类型。

        只回收当前仍归属该实例的登记，类型被更晚的登记方接管后不受本次回收波及。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            service_instance_registry.unregister_owner(key)
        except Exception as error:
            logger.error(f"撤销插件实例 {key} 服务实例登记出错：{str(error)}")

    def _sync_plugin_meta_parsers(self, key: str) -> None:
        """按插件实例当前的声明重建其在名称解析器注册表中的登记。

        解析环绑在声明它的实例上，同步粒度即单个实例：先回收该实例此前的登记，
        避免声明缩减后残留，再按当前声明逐条登记，单条登记失败不影响其余声明。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            declared = self._plugin_projection().provided_meta_parsers(key)
            meta_parser_registry.unregister_owner(key)
            for item in declared.get(key, []):
                try:
                    parser_id, name = declaration_meta_parser_identity(item)
                    meta_parser_registry.register(
                        parser_id,
                        declaration_impl(item),
                        name=name,
                        priority=declaration_meta_parser_priority(item) or 0,
                        owner=key,
                        distribution=ExtensionDistribution.MARKET,
                    )
                except Exception as error:
                    logger.error(f"登记插件实例 {key} 的名称解析器声明出错：{str(error)}")
        except Exception as error:
            logger.error(f"同步插件实例 {key} 名称解析器登记出错：{str(error)}")

    @staticmethod
    def _revoke_plugin_meta_parsers(key: str) -> None:
        """撤销插件实例登记的名称解析器。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            meta_parser_registry.unregister_owner(key)
        except Exception as error:
            logger.error(f"撤销插件实例 {key} 名称解析器登记出错：{str(error)}")

    def _sync_plugin_filter_rules(self, key: str) -> None:
        """按插件当前全部运行实例的声明重建其在筛选规则注册表中的登记。

        规则标识与规则组名是扩展级事实，同步粒度与存储族同理是整个插件而不是单个
        实例，胜出方由投影按稳定规则裁决。

        规则与规则组一并登记：两者归属同一实例、同进同退，分两次登记会让注册表在
        两次调用之间短暂持有一个规则组引用着尚未登记的规则的中间态。

        :param key: 触发本次同步的实例键
        :return: 无返回值
        """
        try:
            projection = self._plugin_projection()
            plugin_id = extension_id_of(key)
            rules = projection.provided_filter_rules(plugin_id)
            groups = projection.provided_filter_rule_groups(plugin_id)
            for owner in self._extension_registration_owners(key):
                try:
                    plugin_filter_rule_registry.register(
                        owner,
                        rules=[
                            projection.declared_filter_rule(item)
                            for item in rules.get(owner, [])
                        ],
                        groups=[
                            projection.declared_filter_rule_group(item)
                            for item in groups.get(owner, [])
                        ],
                    )
                except Exception as error:
                    logger.error(f"登记插件实例 {owner} 的筛选规则声明出错：{str(error)}")
        except Exception as error:
            logger.error(f"同步插件实例 {key} 筛选规则登记出错：{str(error)}")

    def _sync_plugin_commands(self, key: str) -> None:
        """按插件当前全部运行实例的声明重建其在命令注册表中的登记。

        命令词是扩展级事实，同步粒度与筛选规则同理是整个插件而不是单个实例，胜出方由
        投影按稳定规则裁决。两条声明来源（声明式与废弃期的裸列表）在投影处已合并，
        因此两者都进同一张表、受同一套跨插件裁决。

        :param key: 触发本次同步的实例键
        :return: 无返回值
        """
        try:
            projection = self._plugin_projection()
            plugin_id = extension_id_of(key)
            declared: Dict[str, List[Tuple[str, dict]]] = {}
            for command in projection.commands(plugin_id):
                owner, cmd = command.get("pid"), command.get("cmd")
                if owner and cmd:
                    declared.setdefault(owner, []).append((cmd, command))
            for owner in self._extension_registration_owners(key):
                try:
                    plugin_command_registry.register(owner, declared.get(owner, []))
                except Exception as error:
                    logger.error(f"登记插件实例 {owner} 的命令声明出错：{str(error)}")
        except Exception as error:
            logger.error(f"同步插件实例 {key} 命令登记出错：{str(error)}")

    @staticmethod
    def _revoke_plugin_commands(key: str) -> None:
        """撤销插件实例登记的远程命令。

        实例停止后其命令实现不再可信，须直接清空登记：命令表在下一次组装时就不再含有
        它们，用户再敲该命令得到「命令不存在」，而不是调用到已卸载的代码。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            plugin_command_registry.unregister_owner(key)
        except Exception as error:
            logger.error(f"撤销插件实例 {key} 命令登记出错：{str(error)}")

    @staticmethod
    def _revoke_plugin_filter_rules(key: str) -> None:
        """撤销插件实例登记的筛选规则与筛选规则组。

        实例停止后其声明不再可信，须直接清空登记：规则集在下一次组装时就不再含有
        它们，插件停用后其规则不会残留在运行期规则集里。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            plugin_filter_rule_registry.unregister_owner(key)
        except Exception as error:
            logger.error(f"撤销插件实例 {key} 筛选规则登记出错：{str(error)}")

    @staticmethod
    def _revoke_plugin_storages(key: str) -> None:
        """撤销插件实例登记的存储标识。

        只回收标识当前仍归属该实例的登记，覆盖了内建后端的登记被回收后，
        对应标识按其最近一次内建登记的快照还原。

        :param key: 实例键
        :return: 无返回值
        """
        try:
            storage_backend_registry.unregister_owner(key)
        except Exception as error:
            logger.error(f"撤销插件实例 {key} 存储登记出错：{str(error)}")

    def _plugin_catalog(self) -> Any:
        """构造绑定当前市场客户端和插件 DTO 映射器的目录应用服务。"""
        return _plugin_catalog_factory(self)

    def get_plugin_commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取插件命令

        聚合 provides_commands() 声明式登记与废弃期的 get_command() 裸列表，前者的条目
        带 impl 与 args_description，后者的条目带 event；两者的 cmd、desc、category、
        data 与 pid 同名同义。
        [{
            "cmd": "/xx",
            "desc": "xxxx",
            "category": "xxxx",
            "data": {},
            "impl": callable,
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
        获取插件定时任务

        聚合 `provides_schedules()` 声明式登记与 `get_service()` 裸列表两条来源，
        同一实例的同一任务标识以声明式为准。

        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron、interval、date、CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数,
            "func_kwargs": {} # 方法参数
            "pid": "" # 归属实例键
        }]

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 任务描述列表
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

    def get_media_sources(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取运行中插件声明的媒体数据源
        :param pid: 插件ID命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 数据源描述列表
        """
        return self._plugin_projection().media_sources(pid)

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

    def get_plugin_action(self, pid: str, action_id: str) -> Dict[str, Any]:
        """
        确定一次插件动作调用应当执行的动作声明

        `get_plugin_actions` 按插件全部运行实例分组返回，同一 `action_id` 可能在
        不同分身上各存在一份、彼此是两个独立的动作；本方法在未显式指定实例时按
        该插件的默认调用目标裁决，只挑出裁决命中的那一个分身，不取登记顺序中的
        第一个。插件未运行、目标分身未声明该动作，或该插件有实例在运行但没有
        已启用的默认调用目标，均以异常呈现，不返回空值。
        :param pid: 实例键，或插件ID（按该插件的默认调用目标裁决）
        :param action_id: 动作ID
        :return: 动作描述字典，含 action_id、name、func、kwargs
        :raises LookupError: 插件未运行、目标分身未声明该动作，
            或该插件有实例在运行但没有已启用的默认调用目标
        """
        target_key = self._resolve_call_target_key(pid)
        if target_key is None:
            raise LookupError(f"插件 {pid} 不存在或未运行")
        groups = self.get_plugin_actions(target_key)
        actions = groups[0].get("actions", []) if groups else []
        action = next((item for item in actions if item.get("action_id") == action_id), None)
        if action is None or not action.get("func"):
            raise LookupError(f"插件 {pid} 未声明动作 {action_id}")
        return action

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

        聚合两条来源：`provides_agent_tools()` 声明式登记（经契约校验）与
        `get_agent_tools()` 裸类列表（后者已进入废弃期，触达即告警一次）；
        同一实例两条来源皆有声明时工具列表合并。
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

            declared_tools = self._plugin_projection().provided_agent_tools(pid)
            ret_tools = []
            for extension_id, plugin in self._matching_instances(pid):
                tools: List[Any] = [
                    declaration_impl(item)
                    for item in declared_tools.get(extension_id, [])
                ]
                # 废弃阶段推进到默认关闭后不再取用旧钩子，按未声明处理；标识列入
                # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
                if (
                    supports_extension_hook(plugin, "get_agent_tools")
                    and deprecation_is_active("plugin.get_agent_tools")
                ):
                    try:
                        if plugin.get_state():
                            legacy_tools = plugin.get_agent_tools()
                            if legacy_tools:
                                deprecation_warn(
                                    "plugin.get_agent_tools", context=extension_id
                                )
                                tools.extend(legacy_tools)
                    except Exception as e:
                        logger.error(f"获取插件 {extension_id} 智能体工具出错：{str(e)}")
                if tools:
                    ret_tools.append({
                        "plugin_id": extension_id,
                        "plugin_name": plugin.plugin_name,
                        "tools": tools
                    })
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
    def get_plugin_remote_entry(
        plugin_id: str, dist_path: str, version: Optional[str] = None
    ) -> str:
        """
        获取插件的远程入口地址
        :param plugin_id: 插件 ID 或实例键，联邦构建产物属于插件本身而非某个
            实例，先降级到插件标识再拼目录，避免实例键的 @ 分隔符指向不存在
            的目录
        :param dist_path: 插件的分发路径
        :param version: 发起本次查询的实例实际绑定并运行的插件版本号；为空时
            回落到插件当前安装版本，兼容不区分实例版本的旧调用方
        :return: 远程入口地址

        联邦构建产物随版本目录走，版本段作为 dist 路径的第一段插入，按传入版本号
        定位对应版本目录，使不同实例各自绑定的版本各自解析到自己的构建产物，不会
        因为都取插件当前安装版本而拿到同一份代码。指定版本的目录不在磁盘上（已被
        回收或从未落地）时回落到插件当前安装版本。静态资源路由的基目录仍是插件
        目录，因此现有的 ".." 与 is_relative_to 校验不变。
        """
        dist_path = dist_path.strip("/")
        normalized_id = extension_id_of(plugin_id).lower()
        plugin_root = settings.ROOT_PATH / "app" / "plugins" / normalized_id
        source_dir = resolve_plugin_version_dir(plugin_root, version=version, migrate=False)
        if source_dir is None and version:
            source_dir = resolve_plugin_version_dir(plugin_root, migrate=False)
        version_segment = (
            source_dir.name if source_dir is not None and source_dir != plugin_root else ""
        )
        path = posixpath.join(
            "plugin",
            "file",
            normalized_id,
            *([version_segment] if version_segment else []),
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

    def _dashboard_requirement(self, instance_key: str, plugin: Any, key: str) -> Any:
        """
        取指定分身上该仪表盘声明的服务实例作用对象

        按实例键而不是插件ID取声明：同一插件的两个分身各自声明各自的仪表盘，按插件ID
        取会读到另一个分身那一份，作用对象随之张冠李戴。

        没声明 ``provides_dashboards`` 的插件整个不走投影：取数在本字段存在之前从不
        触达声明面，为了读一个它根本没有的字段而把整条取数链路绑上投影，等于让旧写法
        的仪表盘随投影一起失败。取声明本身出错同理只记一笔并按未声明处理——作用对象是
        增量能力，读不到它不该让原本取得出数据的仪表盘一并打不开。

        :param instance_key: 已裁决出的运行实例键
        :param plugin: 该实例键对应的运行态插件实例
        :param key: 仪表盘 key，空字符串代表默认仪表盘
        :return: 作用对象声明；该仪表盘未声明或取不到声明时为 None
        """
        if not supports_extension_hook(plugin, "provides_dashboards"):
            return None
        try:
            declarations = self._plugin_projection().provided_dashboards(instance_key)
        except Exception as error:
            logger.warning(f"读取插件[{instance_key}]仪表盘作用对象出错，按未声明处理：{str(error)}")
            return None
        for item in declarations.get(instance_key, []):
            declared_key, _ = declaration_dashboard_identity(item)
            if (declared_key or "") == (key or ""):
                return declaration_service_instance_requirement(item)
        return None

    def get_plugin_dashboard(self, pid: str, key: str, user_agent: str = None,
                             service_instance: str = None) -> Optional[_SchemaPluginDashboard]:
        """
        获取插件仪表盘

        仪表盘声明了作用于哪一族服务实例时，用户选中的实例名解析成立后按关键字交给
        ``get_dashboard``；未选中则按该族的默认调用目标裁决，裁决不出即报错并列出候选。
        实参只在实现签名接得住时才传——与既有的参数个数探测同一条兼容规则，既有仪表盘
        的取数形状因此一字不改。

        :param pid: 实例键，或插件ID
        :param key: 仪表盘 key
        :param user_agent: 请求方 UA
        :param service_instance: 用户选中的服务实例名，未选中时为 None
        :return: 仪表盘数据；插件返回 None 时为 None
        """

        def __get_params_count(func: Callable):
            """
            获取函数的参数信息
            """
            signature = inspect.signature(func)
            return len(signature.parameters)

        # 获取插件实例
        try:
            target_key = self._resolve_call_target_key(pid)
            plugin_instance = (
                self._plugin_registry.instance(target_key) if target_key is not None else None
            )
        except LookupError as err:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))
        if not plugin_instance:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"插件 {pid} 不存在或未加载")

        # 渲染模式
        render_mode, _ = plugin_instance.get_render_mode()
        # 解析本次取数作用于哪台服务实例；未声明作用对象时不参与调用
        requirement = self._dashboard_requirement(target_key, plugin_instance, key)
        resolved_instance = None
        if requirement is not None:
            try:
                resolved_instance = resolve_required_service_instance(requirement, service_instance)
            except LookupError as err:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))
        # 获取插件仪表板
        try:
            # 检查方法的参数个数
            params_count = __get_params_count(plugin_instance.get_dashboard)
            extra = {}
            if resolved_instance is not None and accepts_keyword(
                    plugin_instance.get_dashboard, SERVICE_INSTANCE_PARAM
            ):
                extra[SERVICE_INSTANCE_PARAM] = resolved_instance
            if params_count > 1:
                dashboard: Tuple = plugin_instance.get_dashboard(
                    key=key, user_agent=user_agent, **extra
                )
            elif params_count > 0:
                dashboard: Tuple = plugin_instance.get_dashboard(user_agent=user_agent, **extra)
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
        :param pid: 插件ID或实例键
        :param attr: 属性名
        :return: 属性值；插件未运行或未声明该属性时为 None
        """
        plugin = self._plugin_registry.instance(pid) or self._plugin_registry.any_instance(pid)
        if not plugin:
            return None
        if not hasattr(plugin, attr):
            return None
        return getattr(plugin, attr)

    def run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        运行插件方法
        :param pid: 实例键，或插件ID（按该插件的默认调用目标裁决）
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        :return: 方法返回值；插件未运行或未实现该方法时为 None
        :raises LookupError: 该插件有实例在运行，但没有已启用的默认调用目标
        """
        plugin = self._resolve_call_target(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        return getattr(plugin, method)(*args, **kwargs)

    async def async_run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        异步运行插件方法
        :param pid: 实例键，或插件ID（按该插件的默认调用目标裁决）
        :param method: 方法名
        :param args: 参数
        :param kwargs: 关键字参数
        :return: 方法返回值；插件未运行或未实现该方法时为 None
        :raises LookupError: 该插件有实例在运行，但没有已启用的默认调用目标
        """
        plugin = self._resolve_call_target(pid)
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
        获取所有运行态插件ID，同一插件的多个实例只出现一次
        """
        return self._plugin_registry.running_plugin_ids()

    def get_running_instance_keys(self, pid: Optional[str] = None) -> List[str]:
        """
        获取运行态插件实例键
        :param pid: 插件ID，为空时返回全部运行实例
        """
        if pid:
            return self._plugin_registry.instance_keys(extension_id_of(pid))
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
            # 运行态实例
            instances = self._matching_instances(pid)
            # 基本属性
            plugin = _SchemaPlugin()
            # ID
            plugin.id = pid
            # 安装状态
            if pid in installed_apps:
                plugin.installed = True
            else:
                plugin.installed = False
            # 运行状态，任一实例启用即为启用
            plugin.state = any(
                self._instance_state(key, instance) for key, instance in instances
            )
            # 源码、依赖和加载状态
            plugin.runtime_status = self._plugin_registry.runtime_status(pid)
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

    def get_installed_plugins(self) -> List[_SchemaPlugin]:
        """
        按安装清单投影插件，缺依赖或缺源码而未加载的条目保留可观察占位卡片

        条目顺序取自持久化安装清单，占位卡片出现或后台恢复完成都不改变用户看到的位置。
        :return: 与安装清单同序的插件信息列表
        """
        installed_ids = get_plugin_storage().read(SystemConfigKey.UserInstalledPlugins) or []
        local_by_id = {
            plugin.id: plugin
            for plugin in self.get_local_plugins()
            if plugin.installed and plugin.id
        }
        result: List[_SchemaPlugin] = []
        for plugin_id in installed_ids:
            plugin = local_by_id.get(plugin_id)
            if plugin:
                result.append(plugin)
                continue
            result.append(_SchemaPlugin(
                id=plugin_id,
                plugin_name=plugin_id,
                installed=True,
                state=False,
                runtime_status=self._plugin_registry.runtime_status(plugin_id),
                is_local=True,
            ))
        return result

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
            # 版本化布局下插件源码在版本目录里，包名需带上版本段
            plugin_root = settings.ROOT_PATH / "app" / "plugins" / pid.lower()
            source_dir = resolve_plugin_version_dir(plugin_root, migrate=False)
            if source_dir is None:
                logger.debug(f"{pid} exists: False")
                return False
            # 构建包名
            package_name = plugin_module_name(plugin_root, source_dir)
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

        # 运行状插件，读的是类级属性与整族状态，取任一实例即可
        plugin_obj = self._plugin_registry.instance(pid) or self._plugin_registry.any_instance(pid)
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



def _handle_storage_instance_config_changed(event: Event) -> None:
    """存储实例配置变更后重建插件声明的服务类型登记。

    插件声明的存储按实例位登进存储后端注册表，实例位由该存储类型的配置决定；配置在
    插件运行期间被增删时，登记不随之重建就会与配置对不上——新配的账号按具名令牌取
    不到，已删的账号仍取得到。插件管理器尚未装配时无人持有登记，无须重建。

    :param event: 配置变更事件
    :return: 无返回值
    """
    changed_keys = getattr(getattr(event, "event_data", None), "key", None) or set()
    if SystemConfigKey.Storages.value not in changed_keys:
        return
    manager = PluginManager.get_existing_instance()
    if manager is None:
        return
    manager.resync_declared_service_types()


eventmanager.add_event_listener(
    EventType.ConfigChanged, _handle_storage_instance_config_changed
)
