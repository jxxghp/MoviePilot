from abc import abstractmethod, ABCMeta
from typing import Generic, Tuple, Union, TypeVar, Type, Dict, Optional, Callable, Any, List

from app.helper.serviceconfig import ServiceConfigHelper
from app.schemas import Notification, MessageChannel, NotificationConf, MediaServerConf, DownloaderConf


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


# 定义泛型，用于表示具体的服务类型和配置类型
TService = TypeVar("TService", bound=object)
TConf = TypeVar("TConf")


class ServiceBase(Generic[TService, TConf], metaclass=ABCMeta):
    """
    抽象服务基类，负责服务的初始化、获取实例和配置管理
    """

    def __init__(self):
        """
        初始化 ServiceBase 类的实例
        """
        self._configs: Dict[str, TConf] = {}
        self._instances: Dict[str, TService] = {}

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None):
        """
        初始化服务，获取配置并实例化对应服务

        :param service_name: 服务名称，作为配置匹配的依据
        :param service_type: 服务的类型，可以是类类型（Type[TService]）、工厂函数（Callable）或 None 来跳过实例化
        """
        configs = self.get_configs()
        if not configs:
            return
        for conf in configs:
            if conf.enabled and conf.type == service_name:
                self._configs[conf.name] = conf
                if service_type:
                    # 通过服务类型或工厂函数来创建实例
                    if isinstance(service_type, type):
                        # 如果传入的是类类型，调用构造函数实例化
                        self._instances[conf.name] = service_type(**conf.config)
                    else:
                        # 如果传入的是工厂函数，直接调用工厂函数
                        self._instances[conf.name] = service_type(conf)

    def get_instance(self, name: str) -> Optional[TService]:
        """
        获取服务实例

        :param name: 实例名称
        :return: 返回对应名称的服务实例，若不存在则返回 None
        """
        if not name:
            return None
        return self._instances.get(name)

    @abstractmethod
    def get_configs(self) -> List[TConf]:
        """
        获取服务的配置列表

        :return: 返回配置列表
        """
        pass

    def get_config(self, name: str, ctype: str = None) -> Optional[TConf]:
        """
        获取配置，支持类型过滤

        :param name: 配置名称
        :param ctype: 配置类型，可选，默认不进行类型过滤
        :return: 返回符合条件的配置，若不存在则返回 None
        """
        if not name:
            return None
        conf = self._configs.get(name)
        if not ctype:
            return conf
        return conf if getattr(conf, "type", None) == ctype else None


class _MessageBase(ServiceBase[TService, NotificationConf]):
    """
    消息基类
    """

    def __init__(self):
        """
        初始化消息基类，并设置消息通道
        """
        super().__init__()
        self._channel: Optional[MessageChannel] = None

    def get_configs(self) -> List[NotificationConf]:
        """
        获取消息通知渠道的配置

        :return: 返回消息通知的配置列表
        """
        return ServiceConfigHelper.get_notification_configs()

    def check_message(self, message: Notification, source: str = None) -> bool:
        """
        检查消息渠道及消息类型，判断是否处理消息

        :param message: 要检查的通知消息
        :param source: 消息来源，可选
        :return: 返回布尔值，表示是否处理该消息
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


class _DownloaderBase(ServiceBase[TService, DownloaderConf]):
    """
    下载器基类
    """

    def __init__(self):
        """
        初始化下载器基类，并设置默认服务器
        """
        super().__init__()
        self._default_server: Any = None
        self._default_server_name: Optional[str] = None

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None):
        """
        初始化服务，获取配置并实例化对应服务

        :param service_name: 服务名称，作为配置匹配的依据
        :param service_type: 服务的类型，可以是类类型（Type[TService]）或工厂函数（Callable），用于创建服务实例
        """
        super().init_service(service_name=service_name, service_type=service_type)
        if self._configs:
            for conf in self._configs.values():
                if conf.default:
                    self._default_server_name = conf.name
                    self._default_server = self.get_instance(conf.name)

    def get_instance(self, name: str = None) -> Optional[Any]:
        """
        获取实例，name为空时，返回默认实例

        :param name: 实例名称，可选，默认为 None
        :return: 返回指定名称的实例，若 name 为 None 则返回默认实例
        """
        if name:
            return self._instances.get(name)
        return self._default_server

    def get_configs(self) -> List[DownloaderConf]:
        """
        获取下载器的配置

        :return: 返回下载器配置列表
        """
        return ServiceConfigHelper.get_downloader_configs()


class _MediaServerBase(ServiceBase[TService, MediaServerConf]):
    """
    媒体服务器基类
    """

    def get_configs(self) -> List[MediaServerConf]:
        """
        获取媒体服务器的配置

        :return: 返回媒体服务器配置列表
        """
        return ServiceConfigHelper.get_mediaserver_configs()
