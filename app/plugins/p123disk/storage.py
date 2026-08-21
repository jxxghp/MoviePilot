"""123 云盘存储后端。

一个对象服务一个存储实例：``storage_instance`` 是本对象所属的实例名，``storage_token``
是它对外的地址（``p123`` 或 ``p123@主号``）。凭据按令牌懒读，因此用户在设置页改完账号
无须重启，下一次取用就连到新账号；连接按实例复用，两个账号各自一条连接、各自一份
路径缓存。

``schema`` 必须与声明里的 ``type`` 一致：宿主按它算出本对象的存储令牌，进而读到本实例
自己的那份配置，缺了它整个对象会去读一份根本不存在的配置。
"""

from pathlib import Path
from threading import RLock
from typing import List, Optional, Tuple

from app.schemas import FileItem, StorageQueryError, StorageUsage
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.storage import StorageBase, StorageInstanceSingleton

from . import transfer
from .api import P123Api
from .client import P123AutoClient

# 存储标识，同时是服务实例声明里的类型标识
STORAGE_ID = "p123"


class P123Storage(StorageBase, metaclass=StorageInstanceSingleton):
    """123 云盘的读写实现。"""

    # 存储标识
    schema = STORAGE_ID

    # 支持的整理方式，网盘之间只能整份转移，不支持链接
    transtype = {"move": "移动", "copy": "复制"}

    def __init__(self) -> None:
        """建立本实例的连接槽位与接口封装。"""
        super().__init__()
        self._lock = RLock()
        self._client: Optional[P123AutoClient] = None
        self._credential: Optional[Tuple[str, str]] = None
        self._api = self._build_api()

    def init_storage(self) -> None:
        """丢弃当前连接与路径缓存，下次取用时按最新配置重建。"""
        with self._lock:
            self._client = None
            self._credential = None
            self._api = self._build_api()

    def _build_api(self) -> P123Api:
        """
        建立接口封装

        :return: 绑定本实例连接与存储令牌的接口封装
        """
        return P123Api(self._resolve_client, lambda: self.storage_token)

    def _credentials(self) -> Tuple[str, str]:
        """
        读取本实例配置里的登录凭据

        :return: (账号, 密码) 二元组，未配置的项为空串
        """
        conf = self.get_conf() or {}
        return str(conf.get("passport") or "").strip(), str(conf.get("password") or "")

    def _resolve_client(self) -> P123AutoClient:
        """
        取用本实例的客户端，凭据变化时重建

        :return: 客户端代理
        :raises StorageQueryError: 本实例尚未配置账号密码
        """
        passport, password = self._credentials()
        if not passport or not password:
            raise StorageQueryError(f"存储 {self.storage_token} 尚未配置账号密码")
        with self._lock:
            if self._client is None or self._credential != (passport, password):
                self._client = P123AutoClient(passport, password)
                self._credential = (passport, password)
            return self._client

    def check(self) -> bool:
        """
        检查本实例是否可用

        :return: 账号已配置且能取到空间使用情况时为 True
        """
        try:
            passport, password = self._credentials()
        except Exception as error:
            logger.error(f"【123云盘】读取 {self.storage_token} 的配置失败：{error}")
            return False
        if not passport or not password:
            return False
        return self.usage() is not None

    def list(self, fileitem: FileItem) -> List[FileItem]:
        """
        浏览目录

        :param fileitem: 目录项或文件项
        :return: 子项列表；传入文件项时为该文件的详情单项列表
        """
        return self._api.list(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """
        在指定目录下创建子目录

        :param fileitem: 父目录项
        :param name: 目录名
        :return: 创建出的目录项；创建失败时为 None
        """
        return self._api.create_folder(fileitem, name)

    def get_folder(self, path: Path) -> Optional[FileItem]:
        """
        取得目录，不存在时逐级创建

        :param path: 目录路径
        :return: 目录项；中途创建失败时为 None
        """
        return self._api.get_folder(path)

    def get_item(self, path: Path) -> Optional[FileItem]:
        """
        按路径取得文件或目录

        :param path: 文件或目录路径
        :return: 文件项；不存在或查询失败时为 None
        """
        return self._api.get_item(path)

    def get_item_strict(self, path: Path) -> Optional[FileItem]:
        """
        按路径取得文件或目录，无法确认状态时报错而不是当作不存在

        :param path: 文件或目录路径
        :return: 文件项；确认不存在时为 None
        :raises StorageQueryError: 网络或接口异常导致无法确认目标状态
        """
        return self._api.get_item_strict(path)

    def delete(self, fileitem: FileItem) -> bool:
        """
        把文件或目录移入回收站

        :param fileitem: 待删除的文件项
        :return: 删除成功时为 True
        """
        return self._api.delete(fileitem)

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """
        重命名文件或目录

        :param fileitem: 待重命名的文件项
        :param name: 新名称
        :return: 重命名成功时为 True
        """
        return self._api.rename(fileitem, name)

    def download(self, fileitem: FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件到本地

        落盘路径经基类校验：远端文件名可能带目录片段，直接拼进目标目录会把文件写到
        目录之外。

        :param fileitem: 待下载的文件项
        :param path: 本地保存目录，为空时落到宿主临时目录
        :return: 本地文件路径；文件名不安全、下载失败或用户取消时为 None
        """
        local_path = self._build_download_path(fileitem, path or settings.TEMP_PATH)
        if local_path is None:
            return None
        return transfer.download(self._api, fileitem, local_path)

    def upload(
        self, fileitem: FileItem, path: Path, new_name: Optional[str] = None
    ) -> Optional[FileItem]:
        """
        上传本地文件到指定目录

        :param fileitem: 上传目标目录项
        :param path: 本地文件路径
        :param new_name: 上传后的文件名，为空时沿用本地文件名
        :return: 上传后的文件项；上传失败或用户取消时为 None
        """
        return transfer.upload(self._api, fileitem, path, new_name)

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        """
        取得文件详情

        :param fileitem: 文件项
        :return: 带完整信息的文件项；查询失败时为 None
        """
        return self._api.detail(fileitem)

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件或目录到指定目录并改名

        :param fileitem: 待复制的文件项
        :param path: 目标目录路径
        :param new_name: 复制后的名称
        :return: 复制并改名都成功时为 True
        """
        return self._api.copy(fileitem, path, new_name)

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件或目录到指定目录并改名

        :param fileitem: 待移动的文件项
        :param path: 目标目录路径
        :param new_name: 移动后的名称
        :return: 移动并改名都成功时为 True
        """
        return self._api.move(fileitem, path, new_name)

    def link(self, fileitem: FileItem, target_file: Path) -> bool:
        """
        硬链接文件，网盘不提供该能力

        :param fileitem: 文件项
        :param target_file: 目标文件路径
        :return: 恒为 False
        """
        return False

    def softlink(self, fileitem: FileItem, target_file: Path) -> bool:
        """
        软链接文件，网盘不提供该能力

        :param fileitem: 文件项
        :param target_file: 目标文件路径
        :return: 恒为 False
        """
        return False

    def usage(self) -> Optional[StorageUsage]:
        """
        取得空间使用情况

        :return: 空间使用情况；查询失败时为 None
        """
        return self._api.usage()
