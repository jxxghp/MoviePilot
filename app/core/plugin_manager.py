import traceback
from threading import Thread
from typing import Tuple, Optional, List, Any

from app.helper import ModuleHelper

from app.core import EventManager
from app.db.systemconfigs import SystemConfigs
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.types import SystemConfigKey


class PluginManager(metaclass=Singleton):
    """
    插件管理器
    """
    systemconfigs: SystemConfigs = None
    eventmanager: EventManager = None

    # 插件列表
    _plugins: dict = {}
    # 运行态插件列表
    _running_plugins: dict = {}
    # 配置Key
    _config_key: str = "plugin.%s"
    # 事件处理线程
    _thread: Thread = None
    # 开关
    _active: bool = False

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.systemconfigs = SystemConfigs()
        self.eventmanager = EventManager()
        # 停止已有插件
        self.stop()
        # 启动插件
        self.start()

    def __run(self):
        """
        事件处理线程
        """
        while self._active:
            event, handlers = self.eventmanager.get_event()
            if event:
                logger.info(f"处理事件：{event.event_type} - {handlers}")
                for handler in handlers:
                    try:
                        names = handler.__qualname__.split(".")
                        self.run_plugin_method(names[0], names[1], event)
                    except Exception as e:
                        logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")

    def start(self):
        """
        启动
        """
        # 加载插件
        self.__load_plugins()

        # 将事件管理器设为启动
        self._active = True
        self._thread = Thread(target=self.__run)
        # 启动事件处理线程
        self._thread.start()

    def stop(self):
        """
        停止
        """
        # 将事件管理器设为停止
        self._active = False
        # 等待事件处理线程退出
        if self._thread:
            self._thread.join()
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
        # 用户已安装插件列表
        user_plugins = self.systemconfigs.get(SystemConfigKey.UserInstalledPlugins) or []
        self._running_plugins = {}
        self._plugins = {}
        for plugin in plugins:
            plugin_id = plugin.__name__
            self._plugins[plugin_id] = plugin
            # 未安装的跳过加载
            if plugin_id not in user_plugins:
                continue
            # 生成实例
            self._running_plugins[plugin_id] = plugin()
            # 初始化配置
            self.reload_plugin(plugin_id)
            logger.info(f"加载插件：{plugin}")

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

    def get_plugin_page(self, pid: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        获取插件额外页面数据
        :return: 标题，页面内容，确定按钮响应函数
        """
        if not self._running_plugins.get(pid):
            return None, None, None
        if not hasattr(self._running_plugins[pid], "get_page"):
            return None, None, None
        return self._running_plugins[pid].get_page()

    def get_plugin_script(self, pid: str) -> Optional[str]:
        """
        获取插件额外脚本
        """
        if not self._running_plugins.get(pid):
            return None
        if not hasattr(self._running_plugins[pid], "get_script"):
            return None
        return self._running_plugins[pid].get_script()

    def get_plugin_state(self, pid: str) -> Optional[bool]:
        """
        获取插件状态
        """
        if not self._running_plugins.get(pid):
            return None
        if not hasattr(self._running_plugins[pid], "get_state"):
            return None
        return self._running_plugins[pid].get_state()

    def save_plugin_config(self, pid: str, conf: dict) -> bool:
        """
        保存插件配置
        """
        if not self._plugins.get(pid):
            return False
        return self.systemconfigs.set(self._config_key % pid, conf)

    @staticmethod
    def __get_plugin_color(plugin: str) -> str:
        """
        获取插件的主题色
        """
        if hasattr(plugin, "plugin_color") and plugin.plugin_color:
            return plugin.plugin_color
        return ""

    def get_plugins_conf(self, auth_level: int) -> dict:
        """
        获取所有插件配置
        """
        all_confs = {}
        for pid, plugin in self._running_plugins.items():
            # 基本属性
            conf = {}
            # 权限
            if hasattr(plugin, "auth_level") \
                    and plugin.auth_level > auth_level:
                continue
            # 名称
            if hasattr(plugin, "plugin_name"):
                conf.update({"name": plugin.plugin_name})
            # 描述
            if hasattr(plugin, "plugin_desc"):
                conf.update({"desc": plugin.plugin_desc})
            # 版本号
            if hasattr(plugin, "plugin_version"):
                conf.update({"version": plugin.plugin_version})
            # 图标
            if hasattr(plugin, "plugin_icon"):
                conf.update({"icon": plugin.plugin_icon})
            # ID前缀
            if hasattr(plugin, "plugin_config_prefix"):
                conf.update({"prefix": plugin.plugin_config_prefix})
            # 插件额外的页面
            if hasattr(plugin, "get_page"):
                title, _, _ = plugin.get_page()
                conf.update({"page": title})
            # 插件额外的脚本
            if hasattr(plugin, "get_script"):
                conf.update({"script": plugin.get_script()})
            # 主题色
            conf.update({"color": self.__get_plugin_color(plugin)})
            # 配置项
            conf.update({"fields": plugin.get_fields() or {}})
            # 配置值
            conf.update({"config": self.get_plugin_config(pid)})
            # 状态
            conf.update({"state": plugin.get_state()})
            # 汇总
            all_confs[pid] = conf
        return all_confs

    def get_plugin_apps(self, auth_level: int) -> dict:
        """
        获取所有插件
        """
        all_confs = {}
        installed_apps = self.systemconfigs.get(SystemConfigKey.UserInstalledPlugins) or []
        for pid, plugin in self._plugins.items():
            # 基本属性
            conf = {}
            # 权限
            if hasattr(plugin, "auth_level") \
                    and plugin.auth_level > auth_level:
                continue
            # ID
            conf.update({"id": pid})
            # 安装状态
            if pid in installed_apps:
                conf.update({"installed": True})
            else:
                conf.update({"installed": False})
            # 名称
            if hasattr(plugin, "plugin_name"):
                conf.update({"name": plugin.plugin_name})
            # 描述
            if hasattr(plugin, "plugin_desc"):
                conf.update({"desc": plugin.plugin_desc})
            # 版本
            if hasattr(plugin, "plugin_version"):
                conf.update({"version": plugin.plugin_version})
            # 图标
            if hasattr(plugin, "plugin_icon"):
                conf.update({"icon": plugin.plugin_icon})
            # 主题色
            conf.update({"color": self.__get_plugin_color(plugin)})
            if hasattr(plugin, "plugin_author"):
                conf.update({"author": plugin.plugin_author})
            # 作者链接
            if hasattr(plugin, "author_url"):
                conf.update({"author_url": plugin.author_url})
            # 汇总
            all_confs[pid] = conf
        return all_confs

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
