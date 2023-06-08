import json
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, Optional

from app.chain import ChainBase
from app.core import settings, Context
from app.db import SessionLocal
from app.db.models import Base
from app.db.models.plugin import PluginData
from app.db.systemconfigs import SystemConfigs
from app.utils.object import ObjectUtils


class PluginChian(ChainBase):
    """
    插件处理链
    """

    def process(self, *args, **kwargs) -> Optional[Context]:
        pass


class _PluginBase(metaclass=ABCMeta):
    """
    插件模块基类，通过继续该类实现插件功能
    除内置属性外，还有以下方法可以扩展或调用：
    - stop_service() 停止插件服务
    - get_config() 获取配置信息
    - update_config() 更新配置信息
    - init_plugin() 生效配置信息
    - get_data_path() 获取插件数据保存目录
    - get_command() 获取插件命令，使用消息机制通过远程控制

    """
    # 插件名称
    plugin_name: str = ""
    # 插件描述
    plugin_desc: str = ""
    
    def __init__(self):
        self.db = SessionLocal()
        self.chain = PluginChian()

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

    def save_data(self, key: str, value: Any) -> Base:
        """
        保存插件数据
        :param key: 数据key
        :param value: 数据值
        """
        if ObjectUtils.is_obj(value):
            value = json.dumps(value)
        plugin = PluginData(plugin_id=self.__class__.__name__, key=key, value=value)
        return plugin.create(self.db)

    def get_data(self, key: str) -> Any:
        """
        获取插件数据
        :param key: 数据key
        """
        data = PluginData.get_plugin_data_by_key(self.db, self.__class__.__name__, key)
        if ObjectUtils.is_obj(data):
            return json.load(data)
        return data
