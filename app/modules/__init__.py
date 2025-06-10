from abc import abstractmethod, ABCMeta
from typing import Generic, Tuple, Union, TypeVar, Type, Dict, Optional, Callable

from app.helper.service import ServiceConfigHelper
from app.schemas import Notification, NotificationConf, MediaServerConf, DownloaderConf
from app.schemas.types import ModuleType, DownloaderType, MediaServerType, MessageChannel, StorageSchema, \
    OtherModulesType


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
    def get_name() -> str:
        """
        获取模块名称
        """
        pass

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        pass

    @staticmethod
    def get_subtype() -> Union[DownloaderType, MediaServerType, MessageChannel, StorageSchema, OtherModulesType]:
        """
        获取模块子类型（下载器、媒体服务器、消息通道、存储类型、其他杂项模块类型）
        """
        pass

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
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
        self._configs: Optional[Dict[str, TConf]] = None
        self._instances: Optional[Dict[str, TService]] = None
        self._service_name: Optional[str] = None

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None):
        """
        初始化服务，获取配置并实例化对应服务

        :param service_name: 服务名称，作为配置匹配的依据
        :param service_type: 服务的类型，可以是类类型（Type[TService]）、工厂函数（Callable）或 None 来跳过实例化
        """
        if not service_name:
            raise Exception("service_name is null")
        self._service_name = service_name
        configs = self.get_configs()
        if configs is None:
            return
        self._configs = configs
        self._instances = {}
        if not service_type:
            return
        for conf in self._configs.values():
            # 通过服务类型或工厂函数来创建实例
            if isinstance(service_type, type):
                # 如果传入的是类类型，调用构造函数实例化
                self._instances[conf.name] = service_type(**conf.config)
            else:
                # 如果传入的是工厂函数，直接调用工厂函数
                self._instances[conf.name] = service_type(conf)

    def get_instances(self) -> Dict[str, TService]:
        """
        获取服务实例列表

        :return: 返回服务实例列表
        """
        return self._instances or {}

    def get_instance(self, name: Optional[str] = None) -> Optional[TService]:
        """
        获取指定名称的服务实例

        :param name: 实例名称，可选。如果为 None，则返回默认实例
        :return: 返回符合条件的服务实例，若不存在则返回 None
        """
        if not self._instances:
            return None
        if name:
            return self._instances.get(name)
        name = self.get_default_config_name()
        return self._instances.get(name) if name else None

    @abstractmethod
    def get_configs(self) -> Dict[str, TConf]:
        """
        获取已启用的服务配置字典

        :return: 返回配置字典
        """
        pass

    def get_config(self, name: Optional[str] = None) -> Optional[TConf]:
        """
        获取指定名称的服务配置

        :param name: 配置名称，可选。如果为 None，则返回默认服务配置
        :return: 返回符合条件的配置，若不存在则返回 None
        """
        if not self._configs:
            return None
        if name:
            return self._configs.get(name)
        name = self.get_default_config_name()
        return self._configs.get(name) if name else None

    def get_default_config_name(self) -> Optional[str]:
        """
        获取默认服务配置的名称

        :return: 默认第一个配置的名称
        """
        # 默认使用第一个配置的名称
        first_conf = next(iter(self._configs.values()), None)
        return first_conf.name if first_conf else None


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

    def get_configs(self) -> Dict[str, NotificationConf]:
        """
        获取已启用的消息通知渠道的配置字典

        :return: 返回消息通知的配置字典
        """
        configs = ServiceConfigHelper.get_notification_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}

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
        初始化下载器基类
        """
        super().__init__()
        self._default_config_name: Optional[str] = None

    def get_default_config_name(self) -> Optional[str]:
        """
        获取默认服务配置的名称

        :return: 优先从所有下载器中查找配置了默认的下载器，如果没有配置，则获取第一个下载器名称
        """
        # 优先查找默认配置
        if self._default_config_name:
            return self._default_config_name

        configs = ServiceConfigHelper.get_downloader_configs()
        for conf in configs:
            if conf.default:
                self._default_config_name = conf.name
                return self._default_config_name
        # 如果没有默认配置，返回第一个配置的名称
        first_conf = next(iter(configs), None)
        self._default_config_name = first_conf.name if first_conf else None
        return self._default_config_name

    def get_configs(self) -> Dict[str, DownloaderConf]:
        """
        获取已启用的下载器的配置字典

        :return: 返回下载器配置字典
        """
        configs = ServiceConfigHelper.get_downloader_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}


class _MediaServerBase(ServiceBase[TService, MediaServerConf]):
    """
    媒体服务器基类
    """

    def get_configs(self) -> Dict[str, MediaServerConf]:
        """
        获取已启用的媒体服务器的配置字典

        :return: 返回媒体服务器配置字典
        """
        configs = ServiceConfigHelper.get_mediaserver_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}
