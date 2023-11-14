import traceback
from typing import List, Any, Dict, Tuple

from app.core.config import settings
from app.core.event import eventmanager
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

    def __init__(self):
        self.siteshelper = SitesHelper()
        self.pluginhelper = PluginHelper()
        self.systemconfig = SystemConfigOper()
        self.install_online_plugin()
        self.init_config()

    def init_config(self):
        # 停止已有插件
        self.stop()
        # 启动插件
        self.start()

    def start(self):
        """
        启动加载插件
        """
        # 扫描插件目录
        plugins = ModuleHelper.load(
            "app.plugins",
            filter_func=lambda _, obj: hasattr(obj, 'init_plugin')
        )
        # 已安装插件
        installed_plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 排序
        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        self._running_plugins = {}
        self._plugins = {}
        for plugin in plugins:
            plugin_id = plugin.__name__
            try:
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
                logger.info(f"Plugin Loaded：{plugin_id}")
                # 设置事件注册状态可用
                eventmanager.enable_events_hander(plugin_id)
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{str(err)} - {traceback.format_exc()}")

    def reload_plugin(self, plugin_id: str, conf: dict):
        """
        重新加载插件
        """
        if not self._running_plugins.get(plugin_id):
            return
        self._running_plugins[plugin_id].init_plugin(conf)

    def stop(self):
        """
        停止
        """
        # 停止所有插件
        for plugin in self._running_plugins.values():
            # 关闭数据库
            if hasattr(plugin, "close"):
                plugin.close()
            # 关闭插件
            if hasattr(plugin, "stop_service"):
                plugin.stop_service()
        # 清空对像
        self._plugins = {}
        self._running_plugins = {}

    def install_online_plugin(self):
        """
        安装本地不存在的在线插件
        """
        if SystemUtils.is_frozen():
            return
        logger.info("开始安装在线插件...")
        # 已安装插件
        install_plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 在线插件
        online_plugins = self.get_online_plugins()
        if not online_plugins:
            logger.error("未获取到在线插件")
            return
        # 支持更新的插件自动更新
        for plugin in online_plugins:
            # 只处理已安装的插件
            if plugin.get("id") in install_plugins and not self.is_plugin_exists(plugin.get("id")):
                # 下载安装
                state, msg = self.pluginhelper.install(pid=plugin.get("id"),
                                                       repo_url=plugin.get("repo_url"))
                # 安装失败
                if not state:
                    logger.error(
                        f"插件 {plugin.get('plugin_name')} v{plugin.get('plugin_version')} 安装失败：{msg}")
                    continue
                logger.info(f"插件 {plugin.get('plugin_name')} 安装成功，版本：{plugin.get('plugin_version')}")
        logger.info("在线插件安装完成")

    def get_plugin_config(self, pid: str) -> dict:
        """
        获取插件配置
        """
        if not self._plugins.get(pid):
            return {}
        return self.systemconfig.get(self._config_key % pid) or {}

    def save_plugin_config(self, pid: str, conf: dict) -> bool:
        """
        保存插件配置
        """
        if not self._plugins.get(pid):
            return False
        return self.systemconfig.set(self._config_key % pid, conf)

    def get_plugin_form(self, pid: str) -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件表单
        """
        if not self._running_plugins.get(pid):
            return [], {}
        if hasattr(self._running_plugins[pid], "get_form"):
            return self._running_plugins[pid].get_form() or ([], {})
        return [], {}

    def get_plugin_page(self, pid: str) -> List[dict]:
        """
        获取插件页面
        """
        if not self._running_plugins.get(pid):
            return []
        if hasattr(self._running_plugins[pid], "get_page"):
            return self._running_plugins[pid].get_page() or []
        return []

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
                ret_commands += plugin.get_command() or []
        return ret_commands

    def get_plugin_apis(self) -> List[Dict[str, Any]]:
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
            if hasattr(plugin, "get_api") \
                    and ObjectUtils.check_method(plugin.get_api):
                apis = plugin.get_api() or []
                for api in apis:
                    api["path"] = f"/{pid}{api['path']}"
                ret_apis.extend(apis)
        return ret_apis

    def run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        运行插件方法
        """
        if not self._running_plugins.get(pid):
            return None
        if not hasattr(self._running_plugins[pid], method):
            return None
        return getattr(self._running_plugins[pid], method)(*args, **kwargs)

    def get_plugin_ids(self) -> List[str]:
        """
        获取所有插件ID
        """
        return list(self._plugins.keys())

    def get_online_plugins(self) -> List[dict]:
        """
        获取所有在线插件信息
        """
        # 返回值
        all_confs = []
        if not settings.PLUGIN_MARKET:
            return all_confs
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        # 线上插件列表
        markets = settings.PLUGIN_MARKET.split(",")
        for market in markets:
            online_plugins = self.pluginhelper.get_plugins(market) or {}
            for pid, plugin in online_plugins.items():
                # 运行状插件
                plugin_obj = self._running_plugins.get(pid)
                # 非运行态插件
                plugin_static = self._plugins.get(pid)
                # 基本属性
                conf = {}
                # ID
                conf.update({"id": pid})
                # 安装状态
                if pid in installed_apps and plugin_static:
                    conf.update({"installed": True})
                else:
                    conf.update({"installed": False})
                # 是否有新版本
                conf.update({"has_update": False})
                if plugin_static:
                    installed_version = getattr(plugin_static, "plugin_version")
                    if StringUtils.compare_version(installed_version, plugin.get("version")) < 0:
                        # 需要更新
                        conf.update({"has_update": True})
                # 运行状态
                if plugin_obj and hasattr(plugin_obj, "get_state"):
                    try:
                        state = plugin_obj.get_state()
                    except Exception as e:
                        logger.error(f"获取插件 {pid} 状态出错：{str(e)}")
                        state = False
                    conf.update({"state": state})
                else:
                    conf.update({"state": False})
                # 是否有详情页面
                conf.update({"has_page": False})
                if plugin_obj and hasattr(plugin_obj, "get_page"):
                    if ObjectUtils.check_method(plugin_obj.get_page):
                        conf.update({"has_page": True})
                # 权限
                if plugin.get("level"):
                    conf.update({"auth_level": plugin.get("level")})
                    if self.siteshelper.auth_level < plugin.get("level"):
                        continue
                # 名称
                if plugin.get("name"):
                    conf.update({"plugin_name": plugin.get("name")})
                # 描述
                if plugin.get("description"):
                    conf.update({"plugin_desc": plugin.get("description")})
                # 版本
                if plugin.get("version"):
                    conf.update({"plugin_version": plugin.get("version")})
                # 图标
                if plugin.get("icon"):
                    conf.update({"plugin_icon": plugin.get("icon")})
                # 主题色
                if plugin.get("color"):
                    conf.update({"plugin_color": plugin.get("color")})
                # 作者
                if plugin.get("author"):
                    conf.update({"plugin_author": plugin.get("author")})
                # 仓库链接
                conf.update({"repo_url": market})
                # 本地标志
                conf.update({"is_local": False})
                # 汇总
                all_confs.append(conf)
        # 按插件ID去重
        if all_confs:
            all_confs = list({v["id"]: v for v in all_confs}.values())
        return all_confs

    def get_local_plugins(self) -> List[dict]:
        """
        获取所有本地已下载的插件信息
        """
        # 返回值
        all_confs = []
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        for pid, plugin in self._plugins.items():
            # 运行状插件
            plugin_obj = self._running_plugins.get(pid)
            # 基本属性
            conf = {}
            # ID
            conf.update({"id": pid})
            # 安装状态
            if pid in installed_apps:
                conf.update({"installed": True})
            else:
                conf.update({"installed": False})
            # 运行状态
            if plugin_obj and hasattr(plugin_obj, "get_state"):
                try:
                    state = plugin_obj.get_state()
                except Exception as e:
                    logger.error(f"获取插件 {pid} 状态出错：{str(e)}")
                    state = False
                conf.update({"state": state})
            else:
                conf.update({"state": False})
            # 是否有详情页面
            if hasattr(plugin, "get_page"):
                if ObjectUtils.check_method(plugin.get_page):
                    conf.update({"has_page": True})
                else:
                    conf.update({"has_page": False})
            # 权限
            if hasattr(plugin, "auth_level"):
                conf.update({"auth_level": plugin.auth_level})
                if self.siteshelper.auth_level < plugin.auth_level:
                    continue
            # 名称
            if hasattr(plugin, "plugin_name"):
                conf.update({"plugin_name": plugin.plugin_name})
            # 描述
            if hasattr(plugin, "plugin_desc"):
                conf.update({"plugin_desc": plugin.plugin_desc})
            # 版本
            if hasattr(plugin, "plugin_version"):
                conf.update({"plugin_version": plugin.plugin_version})
            # 图标
            if hasattr(plugin, "plugin_icon"):
                conf.update({"plugin_icon": plugin.plugin_icon})
            # 主题色
            if hasattr(plugin, "plugin_color"):
                conf.update({"plugin_color": plugin.plugin_color})
            # 作者
            if hasattr(plugin, "plugin_author"):
                conf.update({"plugin_author": plugin.plugin_author})
            # 作者链接
            if hasattr(plugin, "author_url"):
                conf.update({"author_url": plugin.author_url})
            # 是否需要更新
            conf.update({"has_update": False})
            # 本地标志
            conf.update({"is_local": True})
            # 汇总
            all_confs.append(conf)
        return all_confs

    @staticmethod
    def is_plugin_exists(pid: str) -> bool:
        """
        判断插件是否存在
        """
        if not pid:
            return False
        plugin_dir = settings.ROOT_PATH / "app" / "plugins" / pid.lower()
        return plugin_dir.exists()
