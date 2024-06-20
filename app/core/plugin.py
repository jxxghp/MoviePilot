import concurrent
import concurrent.futures
import inspect
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
from app.schemas.types import SystemConfigKey
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class PluginMonitorHandler(FileSystemEventHandler):
    # 计时器
    __reload_timer = None
    # 防抖时间间隔
    __debounce_interval = 0.5
    # 最近一次修改时间
    __last_modified = 0
    # 修改间隔
    __timeout = 2

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

        current_time = time.time()
        if current_time - self.__last_modified < self.__timeout:
            return
        self.__last_modified = current_time
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
                # 防抖处理，通过计时器延迟加载
                if self.__reload_timer:
                    self.__reload_timer.cancel()
                self.__reload_timer = threading.Timer(self.__debounce_interval, self.__reload_plugin, [pid])
                self.__reload_timer.start()
        except Exception as e:
            logger.error(f"插件文件修改后重载出错：{str(e)}")

    @staticmethod
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
                # 如果插件具有认证级别且当前认证级别不足，则不进行实例化
                if hasattr(plugin, "auth_level"):
                    plugin.auth_level = plugin.auth_level
                    if self.siteshelper.auth_level < plugin.auth_level:
                        continue
                # 存储Class
                self._plugins[plugin_id] = plugin
                # 未安装的不加载
                if plugin_id not in installed_plugins:
                    # 设置事件状态为不可用
                    eventmanager.disable_events_hander(plugin_id)
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
                    eventmanager.enable_events_hander(plugin_id)
                else:
                    eventmanager.disable_events_hander(plugin_id)
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{str(err)} - {traceback.format_exc()}")

    def init_plugin(self, plugin_id: str, conf: dict):
        """
        初始化插件
        :param plugin_id: 插件ID
        :param conf: 插件配置
        """
        if not self._running_plugins.get(plugin_id):
            return
        self._running_plugins[plugin_id].init_plugin(conf)
        if self._running_plugins[plugin_id].get_state():
            # 设置启用的插件事件注册状态可用
            eventmanager.enable_events_hander(plugin_id)
        else:
            # 设置事件状态为不可用
            eventmanager.disable_events_hander(plugin_id)

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
            self.__stop_plugin(plugin)
        # 清空对像
        if pid:
            # 清空指定插件
            if pid in self._running_plugins:
                self._running_plugins.pop(pid)
            if pid in self._plugins:
                self._plugins.pop(pid)
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

    def install_online_plugin(self):
        """
        安装本地不存在的在线插件
        """
        if SystemUtils.is_frozen():
            return
        logger.info("开始安装第三方插件...")
        # 已安装插件
        install_plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 在线插件
        online_plugins = self.get_online_plugins()
        if not online_plugins:
            logger.error("未获取到第三方插件")
            return
        # 支持更新的插件自动更新
        for plugin in online_plugins:
            # 只处理已安装的插件
            if plugin.id in install_plugins and not self.is_plugin_exists(plugin.id):
                # 下载安装
                state, msg = self.pluginhelper.install(pid=plugin.id,
                                                       repo_url=plugin.repo_url)
                # 安装失败
                if not state:
                    logger.error(
                        f"插件 {plugin.plugin_name} v{plugin.plugin_version} 安装失败：{msg}")
                    continue
                logger.info(f"插件 {plugin.plugin_name} 安装成功，版本：{plugin.plugin_version}")
        logger.info("第三方插件安装完成")

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

    def get_plugin_dashboard(self, pid: str, key: str, **kwargs) -> Optional[schemas.PluginDashboard]:
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

    def get_plugin_commands(self) -> List[Dict[str, Any]]:
        """
        获取插件命令
        [{
            "cmd": "/xx",
            "event": EventType.xx,
            "desc": "xxxx",
            "data": {}
        }]
        """
        ret_commands = []
        for _, plugin in self._running_plugins.items():
            if hasattr(plugin, "get_command") \
                    and ObjectUtils.check_method(plugin.get_command):
                try:
                    ret_commands += plugin.get_command() or []
                except Exception as e:
                    logger.error(f"获取插件命令出错：{str(e)}")
        return ret_commands

    def get_plugin_apis(self, plugin_id: str = None) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API名称",
            "description": "API说明"
        }]
        """
        ret_apis = []
        for pid, plugin in self._running_plugins.items():
            if plugin_id and pid != plugin_id:
                continue
            if hasattr(plugin, "get_api") \
                    and ObjectUtils.check_method(plugin.get_api):
                try:
                    apis = plugin.get_api() or []
                    for api in apis:
                        api["path"] = f"/{pid}{api['path']}"
                    ret_apis.extend(apis)
                except Exception as e:
                    logger.error(f"获取插件 {pid} API出错：{str(e)}")
        return ret_apis

    def get_plugin_services(self) -> List[Dict[str, Any]]:
        """
        获取插件服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron、interval、date、CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwagrs": {} # 定时器参数
        }]
        """
        ret_services = []
        for pid, plugin in self._running_plugins.items():
            if hasattr(plugin, "get_service") \
                    and ObjectUtils.check_method(plugin.get_service):
                try:
                    services = plugin.get_service()
                    if services:
                        ret_services.extend(services)
                except Exception as e:
                    logger.error(f"获取插件 {pid} 服务出错：{str(e)}")
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

        def __get_plugin_info(market: str) -> Optional[List[schemas.Plugin]]:
            """
            获取插件信息
            """
            online_plugins = self.pluginhelper.get_plugins(market) or {}
            if not online_plugins:
                logger.warn(f"获取插件库失败：{market}")
                return
            ret_plugins = []
            add_time = len(online_plugins)
            for pid, plugin_info in online_plugins.items():
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
                # 权限
                if plugin_info.get("level"):
                    plugin.auth_level = plugin_info.get("level")
                    if self.siteshelper.auth_level < plugin.auth_level:
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

        if not settings.PLUGIN_MARKET:
            return []
        # 返回值
        all_plugins = []
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 使用多线程获取线上插件
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for m in settings.PLUGIN_MARKET.split(","):
                if not m:
                    continue
                futures.append(executor.submit(__get_plugin_info, m))
            for future in concurrent.futures.as_completed(futures):
                plugins = future.result()
                if plugins:
                    all_plugins.extend(plugins)
        # 去重
        all_plugins = list({f"{p.id}{p.plugin_version}": p for p in all_plugins}.values())
        # 所有插件按repo在设置中的顺序排序
        all_plugins.sort(
            key=lambda x: settings.PLUGIN_MARKET.split(",").index(x.repo_url) if x.repo_url else 0
        )
        # 相同ID的插件保留版本号最大版本
        max_versions = {}
        for p in all_plugins:
            if p.id not in max_versions or StringUtils.compare_version(p.plugin_version, max_versions[p.id]) > 0:
                max_versions[p.id] = p.plugin_version
        result = [p for p in all_plugins if
                  p.plugin_version == max_versions[p.id]]
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
            # 权限
            if hasattr(plugin_class, "auth_level"):
                plugin.auth_level = plugin_class.auth_level
                if self.siteshelper.auth_level < plugin.auth_level:
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
        判断插件是否在本地文件系统存在
        :param pid: 插件ID
        """
        if not pid:
            return False
        plugin_dir = settings.ROOT_PATH / "app" / "plugins" / pid.lower()
        return plugin_dir.exists()
