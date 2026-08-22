import threading
from abc import abstractmethod, ABCMeta
from typing import Generic, Tuple, Union, TypeVar, Type, Dict, Optional, Callable
from pathlib import Path

from app.runtime.extensions.contract.instance import describe_instance_candidates
from app.runtime.extensions.projection.module_declarations import builtin_multi_instance
from app.runtime.extensions.service_config import (
    ServiceConfigHelper,
    create_service_instance,
    select_instance_configs,
    service_capability_configs,
)
from app.runtime.log import logger
from app.schemas.file import FileURI
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.system import MediaServerConf
from app.schemas.system import DownloaderConf
from app.schemas.types import ModuleType, NotificationChannel, SystemConfigKey
from app.runtime.reload import ConfigReloadMixin


class _ModuleBase(ConfigReloadMixin, metaclass=ABCMeta):
    """
    模块基类，实现对应方法，在有需要时会被自动调用，返回None代表不启用该模块，将继续执行下一模块
    输入参数与输出参数一致的，或没有输出的，可以被多个模块重复实现
    """

    # Host Module 的配置事件由统一 Adapter 协调，避免同一 generation 被双重重载。
    CONFIG_RELOAD_MANAGED_EXTERNALLY = True

    def __init__(self) -> None:
        """初始化模块生命周期锁"""
        super().__init__()
        self._reload_lock = threading.RLock()

    def on_config_changed(self) -> None:
        """串行停止旧资源并按最新配置重新初始化模块"""
        with self._reload_lock:
            try:
                self.stop()
            except Exception as err:
                logger.error(
                    f"停止 {self.get_reload_name()} 旧资源失败，继续按最新配置初始化：{err}"
                )
            self.init_module()

    def get_reload_name(self):
        return self.get_name()

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

    # 本族服务的能力标签，决定 get_configs() 从哪一族用户配置里取值
    SERVICE_CAPABILITY: Optional[str] = None

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

        单条配置构造失败只跳过它自己，同类型下其余配置照常产出实例：一条连不上的
        配置不应让整个模块连同其余可用实例一起失效。

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
        impl = service_type if isinstance(service_type, type) else None
        factory = None if impl is not None else service_type
        for name, conf in self._configs.items():
            try:
                self._instances[name] = create_service_instance(
                    name, conf, impl=impl, factory=factory
                )
            except Exception as err:
                logger.error(f"{service_name} 实例 {name} 构造失败，已跳过：{err}")

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
        :raises LookupError: 未指定名称，且本模块名下无法确定唯一的默认配置
        """
        if not self._instances:
            return None
        if name:
            return self._instances.get(name)
        name = self.get_default_config_name()
        return self._instances.get(name) if name else None

    def get_configs(self) -> Dict[str, TConf]:
        """
        获取本模块名下已启用的服务配置字典

        按本族能力标签读取用户配置，只保留类型与本模块一致、已启用且具名的配置。
        三族的差别只在能力标签，筛选规则不逐族重复。

        本类型能配几份取自本模块 `capability.toml` 的声明，与扩展声明的类型同规格：
        清单没声明的类型按多实例处置，与该字段出现之前的行为一致。

        :return: 返回配置字典 ``{配置名称: 配置}``
        """
        declared = builtin_multi_instance(self.SERVICE_CAPABILITY, self._service_name)
        return select_instance_configs(
            service_capability_configs(self.SERVICE_CAPABILITY),
            self._service_name,
            capability=self.SERVICE_CAPABILITY,
            multi_instance=True if declared is None else declared,
        )

    def get_config(self, name: Optional[str] = None) -> Optional[TConf]:
        """
        获取指定名称的服务配置

        :param name: 配置名称，可选。如果为 None，则返回默认服务配置
        :return: 返回符合条件的配置，若不存在则返回 None
        :raises LookupError: 未指定名称，且本模块名下无法确定唯一的默认配置
        """
        if not self._configs:
            return None
        if name:
            return self._configs.get(name)
        name = self.get_default_config_name()
        return self._configs.get(name) if name else None

    def _describe_configs(self) -> str:
        """
        列出本模块名下可供显式指定的配置名及其启用状态

        :return: 候选描述文案，一个配置都没有时为「无」
        """
        return describe_instance_candidates(
            (conf.name, bool(getattr(conf, "enabled", True)))
            for conf in (self._configs or {}).values()
        )

    def get_default_config_name(self) -> Optional[str]:
        """
        获取默认服务配置的名称

        本族的默认标记字段虽已存在，但配置界面还没有指定它的入口，此刻一律读到假值，
        因此只在本模块名下恰好只有一条已启用配置时才能确定目标——此时结果与登记顺序
        无关，不构成替调用方做选择。有多条时不按顺序取第一条。

        :return: 默认配置的名称；本模块名下没有已启用配置时为 None
        :raises LookupError: 本模块名下有多条已启用配置，无法确定默认配置
        """
        configs = list((self._configs or {}).values())
        if not configs:
            return None
        if len(configs) == 1:
            return configs[0].name
        raise LookupError(
            f"{self._service_name} 有多个已启用配置，调用必须显式指定名称；"
            f"可选配置：{self._describe_configs()}"
        )


class _MessageBase(ServiceBase[TService, NotificationConf]):
    """
    消息基类
    """
    CONFIG_WATCH = {SystemConfigKey.Notifications.value}
    SERVICE_CAPABILITY = ModuleType.Notification.value

    def __init__(self):
        """
        初始化消息基类，并设置消息通道
        """
        super().__init__()
        self._channel: Optional[NotificationChannel] = None

    def check_message(self, message: Message, source: str = None) -> bool:
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
        # 不是定向发送时，检查消息类型开关
        if not message.userid and message.mtype:
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
    CONFIG_WATCH = {SystemConfigKey.Downloaders.value}
    SERVICE_CAPABILITY = ModuleType.Downloader.value

    def __init__(self):
        """
        初始化下载器基类
        """
        super().__init__()
        self._default_config_name: Optional[str] = None

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None):
        """
        初始化服务，获取配置并实例化对应服务

        :param service_name: 服务名称，作为配置匹配的依据
        :param service_type: 服务的类型，可以是类类型（Type[TService]）、工厂函数（Callable）或 None 来跳过实例化
        """
        # 重置默认配置名称
        self.reset_default_config_name()
        # 初始化服务
        super().init_service(service_name, service_type)

    def get_default_config_name(self) -> Optional[str]:
        """
        获取本模块名下默认下载器配置的名称

        默认标记在全部下载器中全局唯一，而本模块只持有自身类型的实例，因此默认归属其它
        类型时本模块没有默认下载器，返回 None 让该类型的模块认领；标记已停用等同于用户
        选定的目标当前不可用，一律报错而不改走另一个下载器。从未标记过默认时，只在全部
        已启用下载器恰好只有一条的前提下认定它就是目标，多于一条不按顺序取第一条。

        :return: 默认下载器配置的名称；默认归属其它类型或没有任何已启用下载器时为 None
        :raises LookupError: 本模块名下有已启用配置，但全部下载器中没有可用的默认标记
        """
        if self._default_config_name:
            return self._default_config_name

        configs = ServiceConfigHelper.get_downloader_configs()
        enabled_configs = [conf for conf in configs if conf.enabled]
        marked = next((conf for conf in configs if conf.default), None)

        if marked is not None and marked.enabled:
            if marked.type != self._service_name:
                return None
            self._default_config_name = marked.name
            return self._default_config_name

        if marked is None and len(enabled_configs) == 1:
            sole_conf = enabled_configs[0]
            if sole_conf.type != self._service_name:
                return None
            self._default_config_name = sole_conf.name
            return self._default_config_name

        # 本模块名下无配置可用时保持沉默，让确实持有候选的模块给出报错
        if not self._configs:
            return None

        candidates = describe_instance_candidates(
            (conf.name, bool(conf.enabled)) for conf in configs
        )
        if marked is not None:
            raise LookupError(
                f"默认下载器 {marked.name} 已停用，调用必须显式指定下载器；"
                f"可选下载器：{candidates}"
            )
        raise LookupError(
            f"存在多个已启用下载器但未设置默认下载器，调用必须显式指定下载器；"
            f"可选下载器：{candidates}"
        )

    def reset_default_config_name(self):
        """
        重置默认配置名称
        """
        self._default_config_name = None

    @staticmethod
    def __replace_path_prefix(path: Union[Path, str], source: str, target: str) -> Optional[str]:
        """
        按完整路径段替换路径前缀，避免 /media 误匹配 /media2 这类相邻目录。
        """
        if not source or not source.strip() or not target or not target.strip():
            return None

        path_text = Path(path).as_posix()
        source_path = Path(source.strip()).as_posix()
        target_path = Path(target.strip()).as_posix()
        if path_text == source_path:
            return target_path

        source_prefix = f"{source_path.rstrip('/')}/"
        if path_text.startswith(source_prefix):
            suffix = path_text[len(source_prefix):]
            return (Path(target_path) / suffix).as_posix()
        return None

    @staticmethod
    def __strip_storage_prefix(path: str) -> str:
        """
        去掉存储协议前缀 if any，下载器无法识别本地存储协议。
        """
        return FileURI.split_uri(path)[1]

    def normalize_path(self, path: Path, downloader: Optional[str]) -> str:
        """
        根据下载器配置和路径映射，规范化下载路径

        :param path: 存储路径
        :param downloader: 下载器名称
        :return: 规范化后发送给下载器的路径
        """
        normalized_path = path.as_posix()
        conf = self.get_config(downloader)
        if conf and conf.path_mapping:
            for (storage_path, download_path) in conf.path_mapping:
                mapped_path = self.__replace_path_prefix(normalized_path, storage_path, download_path)
                if mapped_path:
                    normalized_path = mapped_path
                    break
        return self.__strip_storage_prefix(normalized_path)

    def normalize_return_path(self, path: Path, downloader: Optional[str]) -> str:
        """
        将下载器返回的路径反向映射为 MoviePilot 可访问的存储路径。

        :param path: 下载器返回的路径
        :param downloader: 下载器名称
        :return: MoviePilot 可访问的路径
        """
        normalized_path = path.as_posix()
        conf = self.get_config(downloader)
        if conf and conf.path_mapping:
            for (storage_path, download_path) in conf.path_mapping:
                mapped_path = self.__replace_path_prefix(normalized_path, download_path, storage_path)
                if mapped_path:
                    normalized_path = mapped_path
                    break
        return self.__strip_storage_prefix(normalized_path)


class _MediaServerBase(ServiceBase[TService, MediaServerConf]):
    """
    媒体服务器基类
    """
    CONFIG_WATCH = {SystemConfigKey.MediaServers.value}
    SERVICE_CAPABILITY = ModuleType.MediaServer.value
