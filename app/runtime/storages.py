"""存储配置的宿主服务端口。

扩展层按本协议读写各存储类型自己的配置项，实现由组合根注入。
"""

from typing import Optional, Protocol, runtime_checkable

from app.runtime.hostport import HostPort
from app.schemas.system import StorageConf


@runtime_checkable
class StorageConfigProvider(Protocol):
    """单个存储类型配置的读写协议。"""

    def get_storage(self, storage: str) -> Optional[StorageConf]:
        """
        获取指定存储的配置。

        :param storage: 存储类型
        :return: 存储配置；未配置时为 None
        """
        ...

    def set_storage(self, storage: str, conf: dict) -> None:
        """
        写入指定存储的配置。

        :param storage: 存储类型
        :param conf: 存储配置内容
        """
        ...

    def reset_storage(self, storage: str) -> None:
        """
        清空指定存储的配置内容。

        :param storage: 存储类型
        """
        ...


storage_config_port: HostPort[StorageConfigProvider] = HostPort("存储配置")
