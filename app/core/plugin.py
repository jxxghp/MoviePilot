import traceback
from typing import List, Any, Dict

from app.db.systemconfig_oper import SystemConfigOper
from app.helper.module import ModuleHelper
from app.log import logger
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
        # 排序
        plugins.sort(key=lambda x: x.plugin_order if hasattr(x, "plugin_order") else 0)
        self._running_plugins = {}
        self._plugins = {}
        for plugin in plugins:
            plugin_id = plugin.__name__
            try:
                # 存储Class
                self._plugins[plugin_id] = plugin
                # 生成实例
                plugin_obj = plugin()
                # 生效插件配置
                plugin_obj.init_plugin(self.get_plugin_config(plugin_id))
                # 存储运行实例
                self._running_plugins[plugin_id] = plugin_obj
                logger.info(f"Plugin Loaded：{plugin_id}")
            except Exception as err:
                logger.error(f"加载插件 {plugin_id} 出错：{err} - {traceback.format_exc()}")

    def stop(self):
        """
        停止
        """
        # 停止所有插件
        for plugin in self._running_plugins.values():
            if hasattr(plugin, "stop"):
                plugin.stop()

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
                ret_commands += plugin.get_command()
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
                apis = plugin.get_api()
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
