from abc import abstractmethod, ABCMeta
from typing import Tuple, Union, Dict, Any, Optional

from app.schemas import Notification, MessageChannel, NotificationConf


class _ModuleBase(metaclass=ABCMeta):
    """
    模块基类，实现对应方法，在有需要时会被自动调用，返回None代表不启用该模块，将继续执行下一模块
    输入参数与输出参数一致的，或没有输出的，可以被多个模块重复实现
    """

    @abstractmethod
    def init_module(self) -> None:
        """
        模块初始化
        """
        pass

    @abstractmethod
    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        模块开关设置，返回开关名和开关值，开关值为True时代表有值即打开，不实现该方法或返回None代表不使用开关
        部分模块支持同时开启多个，此时设置项以,分隔，开关值使用in判断
        """
        pass

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        如果关闭时模块有服务需要停止，需要实现此方法
        :return: None，该方法可被多个模块同时处理
        """
        pass

    @abstractmethod
    def test(self) -> Optional[Tuple[bool, str]]:
        """
        模块测试, 返回测试结果和错误信息
        """
        pass


class _MessageBase:
    """
    消息基类
    """

    _channel: MessageChannel = None
    _configs: Dict[str, NotificationConf] = {}
    _clients: Dict[str, Any] = {}

    def get_client(self, name: str) -> Optional[Any]:
        """
        获取客户端
        """
        if not name:
            return None
        return self._clients.get(name)

    def get_config(self, name: str) -> Optional[NotificationConf]:
        """
        获取配置
        """
        if not name:
            return None
        return self._configs.get(name)

    def checkMessage(self, message: Notification, source: str = None) -> bool:
        """
        检查消息渠道及消息类型，如不符合则不处理
        """
        # 检查消息渠道
        if message.channel and message.channel != self._channel:
            return False
        # 检查消息来源
        if message.source and message.source != source:
            return False
        # 检查消息类型开关
        if message.mtype:
            conf = self.get_config(source)
            if conf:
                switchs = conf.switchs or []
                if message.mtype.value not in switchs:
                    return False
        return True


class _DownloaderBase:
    """
    下载器基类
    """

    _servers: Dict[str, Any] = {}
    _default_server: Any = None
    _default_server_name: str = None

    def get_server(self, name: str = None) -> Optional[Any]:
        """
        获取服务器，name为空则返回默认服务器
        """
        if name:
            return self._servers.get(name)
        return self._default_server


class _MediaServerBase:
    """
    媒体服务器基类
    """

    _servers: Dict[str, Any] = {}

    def get_server(self, name: str) -> Optional[Any]:
        """
        获取Plex服务器
        """
        return self._servers.get(name)
