"""服务实例配置应用服务与组合根注入点。

下载器、媒体服务器与消息渠道的实例配置由服务实例配置表承载。表在持久化层、消费方在
运行期与接口层，本服务是两者之间那一层：对外只收发「整族配置列表」这一种形状——与这
三族配置在设置页上的形状一致——对内负责摊平成行、按消费方分列、以及整族覆盖。

读取带一层进程内缓存。取服务是热路径，每次取用都会重读整族配置；系统设置本来就常驻
内存（``SystemConfigOper`` 在构造时一次性载入），切到表后若每次取用都查一次库，换来的
是一条比原先慢的热路径。缓存只在本服务的写入口失效，绕过本服务直接改库不在其内。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Protocol

from app.application.configuration import get_configured_system_config
from app.runtime.extensions.service_config import service_capability
from app.runtime.extensions.service_config_validation import service_config_records


class ServiceConfigRepository(Protocol):
    """服务实例配置所需的最小持久化端口。"""

    def list_payloads(self, capability: str) -> List[dict]:
        """列出某族全部实例配置的摊平形状。"""

    def replace_capability(self, capability: str, records: List[dict]) -> int:
        """用给定的整族配置覆盖某族现有配置。"""


class ServiceInstanceConfigService:
    """服务实例配置的整族读写应用服务。"""

    def __init__(self, repository: ServiceConfigRepository) -> None:
        """注入服务实例配置数据端口。"""
        self._repository = repository
        self._lock = threading.RLock()
        self._cache: Dict[str, List[dict]] = {}

    def read(self, capability: Optional[str]) -> List[dict]:
        """
        读取某族的全部实例配置。

        :param capability: 族标识
        :return: 该族全部配置字典；族标识为空时为空列表
        """
        if not capability:
            return []
        with self._lock:
            cached = self._cache.get(capability)
            if cached is not None:
                return cached
        payloads = self._repository.list_payloads(capability)
        with self._lock:
            self._cache[capability] = payloads
        return payloads

    def save(self, capability: Optional[str], value: Any) -> bool:
        """
        用给定的整族配置覆盖某族现有配置。

        入参是设置页与智能体交出的整族配置列表，与切表前写进 systemconfig 的形状一致；
        整形（分列、去重、默认调用目标裁决）在写入前完成。

        返回值按「写完之后这一族有没有变」判定，而不是按「有没有执行写入」：配置变更
        事件会触发整族模块重载，一次内容相同的保存不该把服务重启一遍。

        :param capability: 族标识
        :param value: 整族配置列表，为 None 时视为清空该族
        :return: 该族内容是否发生变化；族标识为空时为 False
        """
        if not capability:
            return False
        before = self.read(capability)
        records = service_config_records(capability, value or [])
        self._repository.replace_capability(capability, records)
        self.invalidate(capability)
        return self.read(capability) != before

    def invalidate(self, capability: Optional[str] = None) -> None:
        """
        丢弃读取缓存。

        :param capability: 族标识，为空时丢弃全部族的缓存
        :return: 无返回值
        """
        with self._lock:
            if capability:
                self._cache.pop(capability, None)
            else:
                self._cache.clear()


_configured_service_instance_configs: ServiceInstanceConfigService | None = None


def configure_service_instance_configs(service: ServiceInstanceConfigService) -> None:
    """由启动组合根登记服务实例配置服务。"""
    global _configured_service_instance_configs
    _configured_service_instance_configs = service


def get_configured_service_instance_configs() -> ServiceInstanceConfigService:
    """返回启动阶段登记的服务实例配置服务。"""
    if _configured_service_instance_configs is None:
        raise RuntimeError("服务实例配置服务尚未配置")
    return _configured_service_instance_configs


def read_system_setting(key: Any) -> Any:
    """
    按配置键读取系统设置值。

    三族服务实例配置的事实源已是服务实例配置表，systemconfig 上的同名键只停写不删，
    留作回退用的历史快照，读到的是切表当时的内容。凡是按配置键取值的入口都要走这里
    分流，否则一部分入口读表、另一部分读快照，用户会看到两份互相矛盾的配置。

    :param key: 配置键，接受 `SystemConfigKey` 成员或其取值字符串
    :return: 配置值
    """
    capability = service_capability(getattr(key, "value", key))
    if capability:
        return get_configured_service_instance_configs().read(capability)
    return get_configured_system_config().get(key)


async def async_write_system_setting(key: Any, value: Any) -> bool:
    """
    按配置键写入系统设置值。

    :param key: 配置键，接受 `SystemConfigKey` 成员或其取值字符串
    :param value: 待写入的配置值
    :return: 配置内容是否发生变化
    """
    capability = service_capability(getattr(key, "value", key))
    if capability:
        return get_configured_service_instance_configs().save(capability, value)
    return await get_configured_system_config().async_set(key, value) is True
