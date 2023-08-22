import traceback
from typing import List, Any, Dict, Tuple

from app.db.systemconfig_oper import SystemConfigOper
from app.helper.module import ModuleHelper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton


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
        self.init_config()

    def init_config(self):
        # 配置管理
        self.systemconfig = SystemConfigOper()
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
                    continue
                # 生成实例
                plugin_obj = plugin()
                # 生效插件配置
                plugin_obj.init_plugin(self.get_plugin_config(plugin_id))
                # 存储运行实例
                self._running_plugins[plugin_id] = plugin_obj
                logger.info(f"Plugin Loaded：{plugin_id}")
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{err} - {traceback.format_exc()}")

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
            if hasattr(plugin, "stop_service"):
                plugin.stop_service()

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

    def get_plugin_apps(self) -> List[dict]:
        """
        获取所有插件信息
        """
        # 返回值
        all_confs = []
        # 已安装插件
        installed_apps = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
        for pid, plugin in self._plugins.items():
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
            if pid in self._running_plugins.keys() and hasattr(plugin, "get_state"):
                plugin_obj = self._running_plugins.get(pid)
                conf.update({"state": plugin_obj.get_state()})
            else:
                conf.update({"state": False})
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
            # 汇总
            all_confs.append(conf)
        return all_confs
