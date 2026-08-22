"""存储的宿主契约。

包含两类协议：存储配置的读写端口，实现由组合根注入；
以及文件整理直接调用的存储读写能力，实例由调用方传入。
"""

from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from app.runtime.hostports.port import HostPort
from app.schemas.file import FileItem
from app.schemas.system import StorageConf


@runtime_checkable
class StorageConfigProvider(Protocol):
    """存储实例配置的读写协议。

    一个存储类型可配置多份具名实例，读写按存储令牌进行：裸令牌 ``u115`` 落到该类型
    兼容指针所指的那一份，具名令牌 ``u115@work`` 指该类型下名为 ``work`` 的实例。
    """

    def get_storage(self, storage: str) -> Optional[StorageConf]:
        """
        获取指定存储的配置。

        :param storage: 存储令牌，如 u115 或 u115@work
        :return: 存储配置；未配置时为 None
        """
        ...

    def set_storage(self, storage: str, conf: dict) -> None:
        """
        写入指定存储的配置。

        :param storage: 存储令牌，如 u115 或 u115@work
        :param conf: 存储配置内容
        """
        ...

    def reset_storage(self, storage: str) -> None:
        """
        清空指定存储的配置内容。

        :param storage: 存储令牌，如 u115 或 u115@work
        """
        ...

    def list_storages(self, storage_id: str) -> List[StorageConf]:
        """
        列出指定存储类型的全部实例配置。

        :param storage_id: 存储标识，如 u115
        :return: 该存储类型的实例配置列表
        """
        ...


storage_config_port: HostPort[StorageConfigProvider] = HostPort("存储配置")


@runtime_checkable
class StorageOperations(Protocol):
    """文件整理直接调用的存储读写能力。

    只列出整理过程实际使用的方法；``copy_item`` / ``move_item`` 等可选加速能力
    由调用处按存在与否动态取用，不属于本协议。
    """

    def is_support_transtype(self, transtype: str) -> bool:
        """
        判断存储是否支持某种整理方式。

        :param transtype: 整理方式
        :return: 支持时为 True
        """
        ...

    def list(self, fileitem: FileItem) -> List[FileItem]:
        """
        浏览目录下的文件项。

        :param fileitem: 目录项
        :return: 目录下的文件项列表
        """
        ...

    def get_folder(self, path: Path) -> Optional[FileItem]:
        """
        获取目录，目录不存在时创建。

        :param path: 目录路径
        :return: 目录项；创建失败时为 None
        """
        ...

    def get_item(self, path: Path) -> Optional[FileItem]:
        """
        获取文件或目录。

        :param path: 文件或目录路径
        :return: 文件项；不存在或查询失败时为 None
        """
        ...

    def get_item_strict(self, path: Path) -> Optional[FileItem]:
        """
        获取文件或目录，确认不存在时才返回 None。

        :param path: 文件或目录路径
        :return: 文件项；确认不存在时为 None
        :raises StorageQueryError: 无法确认目标状态
        """
        ...

    def delete(self, fileitem: FileItem) -> bool:
        """
        删除文件或目录。

        :param fileitem: 文件项
        :return: 删除成功时为 True
        """
        ...

    def download(self, fileitem: FileItem, path: Path = None) -> Path:
        """
        下载文件到本地。

        :param fileitem: 文件项
        :param path: 本地保存目录
        :return: 本地临时文件路径
        """
        ...

    def upload(self, fileitem: FileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[FileItem]:
        """
        上传本地文件。

        :param fileitem: 上传目标目录项
        :param path: 本地文件路径
        :param new_name: 上传后的文件名
        :return: 上传后的文件项；失败时为 None
        """
        ...

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件到目标目录。

        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        :return: 复制成功时为 True
        """
        ...

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件到目标目录。

        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        :return: 移动成功时为 True
        """
        ...

    def link(self, fileitem: FileItem, target_file: Path) -> bool:
        """
        为文件创建硬链接。

        :param fileitem: 文件项
        :param target_file: 硬链接路径
        :return: 创建成功时为 True
        """
        ...

    def softlink(self, fileitem: FileItem, target_file: Path) -> bool:
        """
        为文件创建软链接。

        :param fileitem: 文件项
        :param target_file: 软链接路径
        :return: 创建成功时为 True
        """
        ...
