from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, List, Dict, Tuple

from app.chain import ChainBase
from app.core.config import settings
from app.core.event import EventManager
from app.db.plugindata_oper import PluginDataOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.schemas import Notification, NotificationType, MessageChannel


class PluginChian(ChainBase):
    """
    插件处理链
    """
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
    """
    # 插件名称
    plugin_name: str = ""
    # 插件描述
    plugin_desc: str = ""

    def __init__(self):
        # 插件数据
        self.plugindata = PluginDataOper()
        # 处理链
        self.chain = PluginChian()
        # 系统配置
        self.systemconfig = SystemConfigOper()
        # 系统消息
        self.systemmessage = MessageHelper()
        # 事件管理器
        self.eventmanager = EventManager()

    @abstractmethod
    def init_plugin(self, config: dict = None):
        """
        生效配置信息
        :param config: 配置信息字典
        """
        pass

    @staticmethod
    @abstractmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令
        [{
            "cmd": "/xx",
            "event": EventType.xx,
            "desc": "名称",
            "category": "分类，需要注册到Wechat时必须有分类",
            "data": {}
        }]
        """
        pass

    @abstractmethod
    def get_api(self) -> List[Dict[str, Any]]:
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
        pass

    @abstractmethod
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        插件配置页面使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        """
        pass

    @abstractmethod
    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        插件详情页面使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        """
        pass

    @abstractmethod
    def get_state(self) -> bool:
        """
        获取插件运行状态
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
        return self.systemconfig.set(f"plugin.{plugin_id}", config)

    def get_config(self, plugin_id: str = None) -> Any:
        """
        获取配置信息
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return self.systemconfig.get(f"plugin.{plugin_id}")

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

    def save_data(self, key: str, value: Any, plugin_id: str = None):
        """
        保存插件数据
        :param key: 数据key
        :param value: 数据值
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        self.plugindata.save(plugin_id, key, value)

    def get_data(self, key: str, plugin_id: str = None) -> Any:
        """
        获取插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return self.plugindata.get_data(plugin_id, key)

    def del_data(self, key: str, plugin_id: str = None) -> Any:
        """
        删除插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return self.plugindata.del_data(plugin_id, key)

    def post_message(self, channel: MessageChannel = None, mtype: NotificationType = None, title: str = None,
                     text: str = None, image: str = None, link: str = None, userid: str = None):
        """
        发送消息
        """
        self.chain.post_message(Notification(
            channel=channel, mtype=mtype, title=title, text=text,
            image=image, link=link, userid=userid
        ))

    def close(self):
        pass
