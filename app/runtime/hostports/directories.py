"""整理目录配置的宿主服务端口。

扩展层按本协议读取用户配置的下载目录与媒体库目录，实现由组合根注入。
"""

from typing import List, Protocol, runtime_checkable

from app.runtime.hostports.port import HostPort
from app.schemas.system import TransferDirectoryConf


@runtime_checkable
class DirectoryConfigProvider(Protocol):
    """整理目录配置的只读查询协议。"""

    def get_dirs(self) -> List[TransferDirectoryConf]:
        """
        获取全部整理目录配置。

        :return: 目录配置列表
        """
        ...

    def get_library_dirs(self) -> List[TransferDirectoryConf]:
        """
        获取已配置媒体库路径的目录配置。

        :return: 按优先级排序的目录配置列表
        """
        ...

    def get_local_download_dirs(self) -> List[TransferDirectoryConf]:
        """
        获取存储类型为本地的下载目录配置。

        :return: 按优先级排序的目录配置列表
        """
        ...

    def get_local_library_dirs(self) -> List[TransferDirectoryConf]:
        """
        获取存储类型为本地的媒体库目录配置。

        :return: 按优先级排序的目录配置列表
        """
        ...


directory_config_port: HostPort[DirectoryConfigProvider] = HostPort("目录配置")
