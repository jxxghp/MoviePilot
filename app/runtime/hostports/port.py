"""扩展层查询宿主服务的端口槽位。

可插拔扩展需要读取目录配置、站点资源、过滤规则等由应用服务层维护的状态。
扩展只声明所需的最小协议并从槽位取用，具体实现由组合根在启动期注入，
两侧因此不产生静态依赖。
"""

from threading import Lock
from typing import Callable, Generic, Optional, TypeVar

ServiceT = TypeVar("ServiceT")


class HostPort(Generic[ServiceT]):
    """单个宿主服务端口：保存 provider，按需解析实现。"""

    def __init__(self, name: str):
        """
        创建一个尚未注入实现的端口。

        :param name: 端口名称，仅用于未注入时的报错定位
        """
        self._name = name
        self._provider: Optional[Callable[[], ServiceT]] = None
        self._lock = Lock()

    def register(self, provider: Callable[[], ServiceT]) -> None:
        """
        注册端口实现的 provider。

        provider 在每次解析时调用，因此可以是惰性构造，
        也可以返回进程级单例。

        :param provider: 返回端口实现的可调用对象
        """
        with self._lock:
            self._provider = provider

    def reset(self) -> None:
        """清除已注册的 provider，用于测试隔离。"""
        with self._lock:
            self._provider = None

    @property
    def registered(self) -> bool:
        """端口是否已注入实现。"""
        return self._provider is not None

    def resolve(self) -> ServiceT:
        """
        解析端口实现。

        :return: 已注册 provider 返回的实现
        :raises RuntimeError: 组合根尚未注入实现
        """
        provider = self._provider
        if provider is None:
            raise RuntimeError(
                f"宿主服务端口 {self._name} 未注册：请先完成组合根装配"
            )
        return provider()
