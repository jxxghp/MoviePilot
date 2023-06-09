import traceback
from typing import List, Any

from app.db.systemconfigs import SystemConfigs
from app.helper import ModuleHelper
from app.log import logger
from app.utils.singleton import Singleton


class PluginManager(metaclass=Singleton):
    """
    插件管理器
    """
    systemconfigs: SystemConfigs = None

    # 插件列表
    _plugins: dict = {}
    # 运行态插件列表
    _running_plugins: dict = {}
    # 配置Key
    _config_key: str = "plugin.%s"

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.systemconfigs = SystemConfigs()
        # 停止已有插件
        self.stop()
        # 启动插件
        self.start()

    def start(self):
        """
        启动
        """
        # 加载插件
        self.__load_plugins()

    def stop(self):
        """
        停止
        """
        # 停止所有插件
        self.__stop_plugins()

    def __load_plugins(self):
        """
        加载所有插件
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
            self._plugins[plugin_id] = plugin
            # 生成实例
            self._running_plugins[plugin_id] = plugin()
            # 初始化配置
            self.reload_plugin(plugin_id)
            logger.info(f"Plugin Loaded：{plugin.__name__}")

    def reload_plugin(self, pid: str):
        """
        生效插件配置
        """
        if not pid:
            return
        if not self._running_plugins.get(pid):
            return
        if hasattr(self._running_plugins[pid], "init_plugin"):
            try:
                self._running_plugins[pid].init_plugin(self.get_plugin_config(pid))
                logger.debug(f"生效插件配置：{pid}")
            except Exception as err:
                logger.error(f"加载插件 {pid} 出错：{err} - {traceback.format_exc()}")

    def __stop_plugins(self):
        """
        停止所有插件
        """
        for plugin in self._running_plugins.values():
            if hasattr(plugin, "stop"):
                plugin.stop()

    def get_plugin_config(self, pid: str) -> dict:
        """
        获取插件配置
        """
        if not self._plugins.get(pid):
            return {}
        return self.systemconfigs.get(self._config_key % pid) or {}

    def save_plugin_config(self, pid: str, conf: dict) -> bool:
        """
        保存插件配置
        """
        if not self._plugins.get(pid):
            return False
        return self.systemconfigs.set(self._config_key % pid, conf)

    def get_plugin_commands(self) -> List[dict]:
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
            if hasattr(plugin, "get_command"):
                ret_commands.append(plugin.get_command())
        return ret_commands

    def run_plugin_method(self, pid: str, method: str, *args, **kwargs) -> Any:
        """
        运行插件方法
        """
        if not self._running_plugins.get(pid):
            return None
        if not hasattr(self._running_plugins[pid], method):
            return None
        return getattr(self._running_plugins[pid], method)(*args, **kwargs)
