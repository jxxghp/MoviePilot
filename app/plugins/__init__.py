from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any

from app.core import settings
from app.db.systemconfigs import SystemConfigs


class _PluginBase(metaclass=ABCMeta):
    """
    插件模块基类，通过继续该类实现插件功能
    除内置属性外，还有以下方法可以扩展或调用：
    - get_fields() 获取配置字典，用于生成插件配置表单
    - get_state() 获取插件启用状态，用于展示运行状态
    - stop_service() 停止插件服务
    - get_config() 获取配置信息
    - update_config() 更新配置信息
    - init_config() 生效配置信息
    - get_page() 插件额外页面数据，在插件配置页面左下解按钮展示
    - get_script() 插件额外脚本（Javascript），将会写入插件页面，可在插件元素中绑定使用，，XX_PluginInit为初始化函数
    - get_data_path() 获取插件数据保存目录
    - get_command() 获取插件命令，使用消息机制通过远程控制

    """
    # 插件名称
    plugin_name: str = ""
    # 插件描述
    plugin_desc: str = ""
    # 插件图标
    plugin_icon: str = ""
    # 主题色
    plugin_color: str = ""
    # 插件版本
    plugin_version: str = "1.0"
    # 插件作者
    plugin_author: str = ""
    # 作者主页
    author_url: str = ""
    # 插件配置项ID前缀：为了避免各插件配置表单相冲突，配置表单元素ID自动在前面加上此前缀
    plugin_config_prefix: str = "plugin_"
    # 显示顺序
    plugin_order: int = 0
    # 可使用的用户级别
    auth_level: int = 1

    @staticmethod
    @abstractmethod
    def get_fields() -> dict:
        """
        获取配置字典，用于生成表单
        """
        pass

    @abstractmethod
    def get_state(self) -> bool:
        """
        获取插件启用状态
        """
        pass

    @abstractmethod
    def init_plugin(self, config: dict = None):
        """
        生效配置信息
        :param config: 配置信息字典
        """
        pass

    @abstractmethod
    def stop_service(self):
        """
        停止插件
        """
        pass

    def update_config(self, config: dict, plugin_id: str = None) -> bool:
        """
        更新配置信息
        :param config: 配置信息字典
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return SystemConfigs().set(f"plugin.{plugin_id}", config)

    def get_config(self, plugin_id: str = None) -> Any:
        """
        获取配置信息
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return SystemConfigs().get(f"plugin.{plugin_id}")

    def get_data_path(self, plugin_id: str = None) -> Path:
        """
        获取插件数据保存目录
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        data_path = settings.PLUGIN_DATA_PATH / f"{plugin_id}"
        if not data_path.exists():
            data_path.mkdir(parents=True)
        return data_path
