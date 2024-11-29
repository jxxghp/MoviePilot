import concurrent
import concurrent.futures
import importlib.util
import inspect
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager
from app.db.plugindata_oper import PluginDataOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.module import ModuleHelper
from app.helper.plugin import PluginHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas.types import EventType, SystemConfigKey
from app.utils.crypto import RSAUtils
from app.utils.limit import rate_limit_window
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class PluginMonitorHandler(FileSystemEventHandler):

    def on_modified(self, event):
        """
        插件文件修改后重载
        """
        if event.is_directory:
            return
        # 使用 pathlib 处理文件路径，跳过非 .py 文件以及 pycache 目录中的文件
        event_path = Path(event.src_path)
        if not event_path.name.endswith(".py") or "pycache" in event_path.parts:
            return

        # 读取插件根目录下的__init__.py文件，读取class XXXX(_PluginBase)的类名
        try:
            plugins_root = settings.ROOT_PATH / "app" / "plugins"
            # 确保修改的文件在 plugins 目录下
            if plugins_root not in event_path.parents:
                return
            # 获取插件目录路径，没有找到__init__.py时，说明不是有效包，跳过插件重载
            # 插件重载目前没有支持app/plugins/plugin/package/__init__.py的场景，这里也不做支持
            plugin_dir = event_path.parent
            init_file = plugin_dir / "__init__.py"
            if not init_file.exists():
                logger.debug(f"{plugin_dir} 下没有找到 __init__.py，跳过插件重载")
                return

            with open(init_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            pid = None
            for line in lines:
                if line.startswith("class") and "(_PluginBase)" in line:
                    pid = line.split("class ")[1].split("(_PluginBase)")[0].strip()
            if pid:
                self.__reload_plugin(pid)
        except Exception as e:
            logger.error(f"插件文件修改后重载出错：{str(e)}")

    @staticmethod
    @rate_limit_window(max_calls=1, window_seconds=2, source="PluginMonitor", enable_logging=False)
    def __reload_plugin(pid):
        """
        重新加载插件
        """
        try:
            logger.info(f"插件 {pid} 文件修改，重新加载...")
            PluginManager().reload_plugin(pid)
        except Exception as e:
            logger.error(f"插件文件修改后重载出错：{str(e)}")


class PluginManager(metaclass=Singleton):
    """
    插件管理器
    """
    systemconfig: SystemConfigOper = None

    # 插件列表
    _plugins: dict = {}
    # 运行态插件列表
    _running_plugins: dict = {}
    # 配置Key
    _config_key: str = "plugin.%s"
    # 监听器
    _observer: Observer = None

    def __init__(self):
        self.siteshelper = SitesHelper()
        self.pluginhelper = PluginHelper()
        self.systemconfig = SystemConfigOper()
        self.plugindata = PluginDataOper()
        # 开发者模式监测插件修改
        if settings.DEV or settings.PLUGIN_AUTO_RELOAD:
            self.__start_monitor()

    def init_config(self):
        # 停止已有插件
        self.stop()
        # 启动插件
        self.start()

    def start(self, pid: str = None):
        """
        启动加载插件
        :param pid: 插件ID，为空加载所有插件
        """

        def check_module(module: Any):
            """
            检查模块
            """
            if not hasattr(module, 'init_plugin') or not hasattr(module, "plugin_name"):
                return False
            return True

        # 扫描插件目录
        if pid:
            # 加载指定插件
            plugins = ModuleHelper.load_with_pre_filter(
                "app.plugins",
                filter_func=lambda name, obj: check_module(obj) and name == pid
            )
        else:
            # 加载所有插件
            plugins = ModuleHelper.load(
                "app.plugins",
                filter_func=lambda _, obj: check_module(obj)
            )
        # 已安装插件
        installed_plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
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
                # 未安装的不加载
                if plugin_id not in installed_plugins:
                    # 设置事件状态为不可用
                    eventmanager.disable_event_handler(plugin)
                    continue
                # 生成实例
                plugin_obj = plugin()
                # 生效插件配置
                plugin_obj.init_plugin(self.get_plugin_config(plugin_id))
                # 存储运行实例
                self._running_plugins[plugin_id] = plugin_obj
                logger.info(f"加载插件：{plugin_id} 版本：{plugin_obj.plugin_version}")
                # 启用的插件才设置事件注册状态可用
                if plugin_obj.get_state():
                    eventmanager.enable_event_handler(plugin)
                else:
                    eventmanager.disable_event_handler(plugin)
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{str(err)} - {traceback.format_exc()}")

    def init_plugin(self, plugin_id: str, conf: dict):
        """
        初始化插件
        :param plugin_id: 插件ID
        :param conf: 插件配置
        """
        plugin = self._running_plugins.get(plugin_id)
        if not plugin:
            return
        # 初始化插件
        plugin.init_plugin(conf)
        # 检查插件状态并启用/禁用事件处理器
        if plugin.get_state():
            # 启用插件类的事件处理器
            eventmanager.enable_event_handler(type(plugin))
        else:
            # 禁用插件类的事件处理器
            eventmanager.disable_event_handler(type(plugin))

    def stop(self, pid: str = None):
        """
        停止插件服务
        :param pid: 插件ID，为空停止所有插件
        """
        # 停止插件
        if pid:
            logger.info(f"正在停止插件 {pid}...")
        else:
            logger.info("正在停止所有插件...")
        for plugin_id, plugin in self._running_plugins.items():
            if pid and plugin_id != pid:
                continue
            eventmanager.disable_event_handler(type(plugin))
            self.__stop_plugin(plugin)
        # 清空对像
        if pid:
            # 清空指定插件
            if pid in self._running_plugins:
                self._running_plugins.pop(pid)
        else:
            # 清空
            self._plugins = {}
            self._running_plugins = {}
        logger.info("插件停止完成")

    def __start_monitor(self):
        """
        开发者模式下监测插件文件修改
        """
        logger.info("开发者模式下开始监测插件文件修改...")
        monitor_handler = PluginMonitorHandler()
        self._observer = Observer()
        self._observer.schedule(monitor_handler, str(settings.ROOT_PATH / "app" / "plugins"), recursive=True)
        self._observer.start()

    def stop_monitor(self):
        """
        停止监测插件修改
        """
        # 停止监测
        if self._observer:
            logger.info("正在停止插件文件修改监测...")
            self._observer.stop()
            self._observer.join()
            logger.info("插件文件修改监测停止完成")

    @staticmethod
    def __stop_plugin(plugin: Any):
        """
        停止插件
        :param plugin: 插件实例
        """
        # 关闭数据库
        if hasattr(plugin, "close"):
            plugin.close()
        # 关闭插件
        if hasattr(plugin, "stop_service"):
            plugin.stop_service()

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
        # 先移除
        self.stop(plugin_id)
        # 重新加载
        self.start(plugin_id)
        # 广播事件
        eventmanager.send_event(EventType.PluginReload, data={"plugin_id": plugin_id})

    def sync(self) -> List[str]:
        """
        安装本地不存在的在线插件
        """

        def install_plugin(plugin):
            start_time = time.time()
            state, msg = self.pluginhelper.install(pid=plugin.id, repo_url=plugin.repo_url, force_install=True)
            elapsed_time = time.time() - start_time
            if state:
                logger.info(
                    f"插件 {plugin.plugin_name} 安装成功，版本：{plugin.plugin_version}，耗时：{elapsed_time:.2f} 秒")
                sync_plugins.append(plugin.id)
            else:
                logger.error(
                    f"插件 {plugin.plugin_name} v{plugin.plugin_version} 安装失败：{msg}，耗时：{elapsed_time:.2f} 秒")
                failed_plugins.append(plugin.id)

        if SystemUtils.is_frozen():
            return []

        # 获取已安装插件列表
        install_plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 获取在线插件列表
        online_plugins = self.get_online_plugins()
        # 确定需要安装的插件
        plugins_to_install = [
            plugin for plugin in online_plugins
            if plugin.id in install_plugins and not self.is_plugin_exists(plugin.id)
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

    def install_plugin_missing_dependencies(self) -> List[str]:
        """
        安装插件中缺失或不兼容的依赖项
        """
        # 第一步：获取需要安装的依赖项列表
        missing_dependencies = self.pluginhelper.find_missing_dependencies()
        if not missing_dependencies:
            return missing_dependencies
        logger.debug(f"检测到缺失的依赖项: {missing_dependencies}")
        logger.info(f"开始安装缺失的依赖项，共 {len(missing_dependencies)} 个...")
        # 第二步：安装依赖项并返回结果
        total_start_time = time.time()
        success, message = self.pluginhelper.install_dependencies(missing_dependencies)
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
        conf = self.systemconfig.get(self._config_key % pid)
        if conf:
            # 去掉空Key
            return {k: v for k, v in conf.items() if k}
        return {}

    def save_plugin_config(self, pid: str, conf: dict) -> bool:
        """
        保存插件配置
        :param pid: 插件ID
        :param conf: 配置
        """
        if not self._plugins.get(pid):
            return False
        return self.systemconfig.set(self._config_key % pid, conf)

    def delete_plugin_config(self, pid: str) -> bool:
        """
        删除插件配置
        :param pid: 插件ID
        """
        if not self._plugins.get(pid):
            return False
        return self.systemconfig.delete(self._config_key % pid)

    def delete_plugin_data(self, pid: str) -> bool:
        """
        删除插件数据
        :param pid: 插件ID
        """
        if not self._plugins.get(pid):
            return False
        self.plugindata.del_data(pid)
        return True

    def get_plugin_form(self, pid: str) -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件表单
        :param pid: 插件ID
        """
        plugin = self._running_plugins.get(pid)
        if not plugin:
            return [], {}
        if hasattr(plugin, "get_form"):
            return plugin.get_form() or ([], {})
        return [], {}

    def get_plugin_page(self, pid: str) -> List[dict]:
        """
        获取插件页面
        :param pid: 插件ID
        """
        plugin = self._running_plugins.get(pid)
        if not plugin:
            return []
        if hasattr(plugin, "get_page"):
            return plugin.get_page() or []
        return []

    def get_plugin_dashboard(self, pid: str, key: str = None, **kwargs) -> Optional[schemas.PluginDashboard]:
        """
        获取插件仪表盘
        :param pid: 插件ID
        :param key: 仪表盘key
        """

        def __get_params_count(func: Callable):
            """
            获取函数的参数信息
            """
            signature = inspect.signature(func)
            return len(signature.parameters)

        plugin = self._running_plugins.get(pid)
        if not plugin:
            return None
        if hasattr(plugin, "get_dashboard"):
            # 检查方法的参数个数
            params_count = __get_params_count(plugin.get_dashboard)
            if params_count > 1:
                dashboard: Tuple = plugin.get_dashboard(key=key, **kwargs)
            elif params_count > 0:
                dashboard: Tuple = plugin.get_dashboard(**kwargs)
            else:
                dashboard: Tuple = plugin.get_dashboard()
            if dashboard:
                cols, attrs, elements = dashboard
                return schemas.PluginDashboard(
                    id=pid,
                    name=plugin.plugin_name,
                    key=key or "",
                    cols=cols or {},
                    elements=elements,
                    attrs=attrs or {}
                )
        return None

    def get_plugin_state(self, pid: str) -> bool:
        """
        获取插件状态
        :param pid: 插件ID
        """
        plugin = self._running_plugins.get(pid)
        return plugin.get_state() if plugin else False

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
        ret_commands = []
        for plugin_id, plugin in self._running_plugins.items():
            if pid and pid != plugin_id:
                continue
            if hasattr(plugin, "get_command") and ObjectUtils.check_method(plugin.get_command):
                try:
                    if not plugin.get_state():
                        continue
                    commands = plugin.get_command() or []
                    for command in commands:
                        command["pid"] = plugin_id
                    ret_commands.extend(commands)
                except Exception as e:
                    logger.error(f"获取插件命令出错：{str(e)}")
        return ret_commands

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
        ret_apis = []
        for plugin_id, plugin in self._running_plugins.items():
            if pid and pid != plugin_id:
                continue
            if hasattr(plugin, "get_api") and ObjectUtils.check_method(plugin.get_api):
                try:
                    if not plugin.get_state():
                        continue
                    apis = plugin.get_api() or []
                    for api in apis:
                        api["path"] = f"/{plugin_id}{api['path']}"
                    ret_apis.extend(apis)
                except Exception as e:
                    logger.error(f"获取插件 {plugin_id} API出错：{str(e)}")
        return ret_apis

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
        ret_services = []
        for plugin_id, plugin in self._running_plugins.items():
            if pid and pid != plugin_id:
                continue
            if hasattr(plugin, "get_service") and ObjectUtils.check_method(plugin.get_service):
                try:
                    if not plugin.get_state():
                        continue
                    services = plugin.get_service() or []
                    ret_services.extend(services)
                except Exception as e:
                    logger.error(f"获取插件 {plugin_id} 服务出错：{str(e)}")
        return ret_services

    def get_plugin_dashboard_meta(self):
        """
        获取所有插件仪表盘元信息
        """
        dashboard_meta = []
        for plugin_id, plugin in self._running_plugins.items():
            if not hasattr(plugin, "get_dashboard") or not ObjectUtils.check_method(plugin.get_dashboard):
                continue
            try:
                if not plugin.get_state():
                    continue
                # 如果是多仪表盘实现
                if hasattr(plugin, "get_dashboard_meta") and ObjectUtils.check_method(plugin.get_dashboard_meta):
                    meta = plugin.get_dashboard_meta()
                    if meta:
                        dashboard_meta.extend([{
                            "id": plugin_id,
                            "name": m.get("name"),
                            "key": m.get("key"),
                        } for m in meta if m])
                else:
                    dashboard_meta.append({
                        "id": plugin_id,
                        "name": plugin.plugin_name,
                        "key": "",
                    })
            except Exception as e:
                logger.error(f"获取插件[{plugin_id}]仪表盘元数据出错：{str(e)}")
        return dashboard_meta

    def get_plugin_attr(self, pid: str, attr: str) -> Any:
        """
        获取插件属性
        :param pid: 插件ID
        :param attr: 属性名
        """
        plugin = self._running_plugins.get(pid)
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
        plugin = self._running_plugins.get(pid)
        if not plugin:
            return None
        if not hasattr(plugin, method):
            return None
        return getattr(plugin, method)(*args, **kwargs)

    def get_plugin_ids(self) -> List[str]:
        """
        获取所有插件ID
        """
        return list(self._plugins.keys())

    def get_running_plugin_ids(self) -> List[str]:
        """
        获取所有运行态插件ID
        """
        return list(self._running_plugins.keys())

    def get_online_plugins(self) -> List[schemas.Plugin]:
        """
        获取所有在线插件信息
        """
        if not settings.PLUGIN_MARKET:
            return []

        # 返回值
        all_plugins = []
        # 用于存储高于 v1 版本的插件（如 v2, v3 等）
        higher_version_plugins = []
        # 用于存储 v1 版本插件
        base_version_plugins = []

        # 使用多线程获取线上插件
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures_to_version = {}
            for m in settings.PLUGIN_MARKET.split(","):
                if not m:
                    continue
                # 提交任务获取 v1 版本插件，存储 future 到 version 的映射
                base_future = executor.submit(self.get_plugins_from_market, m, None)
                futures_to_version[base_future] = "base_version"

                # 提交任务获取高版本插件（如 v2、v3），存储 future 到 version 的映射
                if settings.VERSION_FLAG:
                    higher_version_future = executor.submit(self.get_plugins_from_market, m, settings.VERSION_FLAG)
                    futures_to_version[higher_version_future] = "higher_version"

            # 按照完成顺序处理结果
            for future in concurrent.futures.as_completed(futures_to_version):
                plugins = future.result()
                version = futures_to_version[future]

                if plugins:
                    if version == "higher_version":
                        higher_version_plugins.extend(plugins)  # 收集高版本插件
                    else:
                        base_version_plugins.extend(plugins)  # 收集 v1 版本插件

        # 优先处理高版本插件
        all_plugins.extend(higher_version_plugins)
        # 将未出现在高版本插件列表中的 v1 插件加入 all_plugins
        higher_plugin_ids = {f"{p.id}{p.plugin_version}" for p in higher_version_plugins}
        all_plugins.extend([p for p in base_version_plugins if f"{p.id}{p.plugin_version}" not in higher_plugin_ids])
        # 去重
        all_plugins = list({f"{p.id}{p.plugin_version}": p for p in all_plugins}.values())
        # 所有插件按 repo 在设置中的顺序排序
        all_plugins.sort(
            key=lambda x: settings.PLUGIN_MARKET.split(",").index(x.repo_url) if x.repo_url else 0
        )
        # 相同 ID 的插件保留版本号最大的版本
        max_versions = {}
        for p in all_plugins:
            if p.id not in max_versions or StringUtils.compare_version(p.plugin_version, max_versions[p.id]) > 0:
                max_versions[p.id] = p.plugin_version
        result = [p for p in all_plugins if p.plugin_version == max_versions[p.id]]
        logger.info(f"共获取到 {len(result)} 个线上插件")
        return result

    def get_local_plugins(self) -> List[schemas.Plugin]:
        """
        获取所有本地已下载的插件信息
        """
        # 返回值
        plugins = []
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        for pid, plugin_class in self._plugins.items():
            # 运行状插件
            plugin_obj = self._running_plugins.get(pid)
            # 基本属性
            plugin = schemas.Plugin()
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
                if ObjectUtils.check_method(plugin_class.get_page):
                    plugin.has_page = True
                else:
                    plugin.has_page = False
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

    @staticmethod
    def is_plugin_exists(pid: str) -> bool:
        """
        判断插件是否在本地包中存在
        :param pid: 插件ID
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
            return package_exists
        except Exception as e:
            logger.debug(f"获取插件是否在本地包中存在失败，{e}")
            return False

    def get_plugins_from_market(self, market: str, package_version: str = None) -> Optional[List[schemas.Plugin]]:
        """
        从指定的市场获取插件信息
        :param market: 市场的 URL 或标识
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        :return: 返回插件的列表，若获取失败返回 []
        """
        if not market:
            return []
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 获取在线插件
        online_plugins = self.pluginhelper.get_plugins(market, package_version) or {}
        if not online_plugins:
            if not package_version:
                logger.warning(f"获取插件库失败：{market}，请检查 GitHub 网络连接")
            return []
        ret_plugins = []
        add_time = len(online_plugins)
        for pid, plugin_info in online_plugins.items():
            # 如 package_version 为空，则需要判断插件是否兼容当前版本
            if not package_version:
                if plugin_info.get(settings.VERSION_FLAG) is not True:
                    # 插件当前版本不兼容
                    continue
            # 运行状插件
            plugin_obj = self._running_plugins.get(pid)
            # 非运行态插件
            plugin_static = self._plugins.get(pid)
            # 基本属性
            plugin = schemas.Plugin()
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
                if StringUtils.compare_version(installed_version, plugin_info.get("version")) < 0:
                    # 需要更新
                    plugin.has_update = True
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
            if plugin_obj and hasattr(plugin_obj, "get_page"):
                if ObjectUtils.check_method(plugin_obj.get_page):
                    plugin.has_page = True
            # 公钥
            if plugin_info.get("key"):
                plugin.plugin_public_key = plugin_info.get("key")
            # 权限
            if not self.__set_and_check_auth_level(plugin=plugin, source=plugin_info):
                continue
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
            if plugin_info.get("labels"):
                plugin.plugin_label = plugin_info.get("labels")
            # 作者
            if plugin_info.get("author"):
                plugin.plugin_author = plugin_info.get("author")
            # 更新历史
            if plugin_info.get("history"):
                plugin.history = plugin_info.get("history")
            # 仓库链接
            plugin.repo_url = market
            # 本地标志
            plugin.is_local = False
            # 添加顺序
            plugin.add_time = add_time
            # 汇总
            ret_plugins.append(plugin)
            add_time -= 1

        return ret_plugins

    def __set_and_check_auth_level(self, plugin: Union[schemas.Plugin, Type[Any]],
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
        if self.siteshelper.auth_level > 1 and plugin.auth_level == 99 and hasattr(plugin, "plugin_public_key"):
            plugin_id = plugin.id if isinstance(plugin, schemas.Plugin) else plugin.__name__
            public_key = plugin.plugin_public_key
            if public_key:
                private_key = PluginManager.__get_plugin_private_key(plugin_id)
                verify = RSAUtils.verify_rsa_keys(public_key=public_key, private_key=private_key)
                return verify
        # 如果当前站点认证级别小于插件级别，则返回 False
        if self.siteshelper.auth_level < plugin.auth_level:
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
