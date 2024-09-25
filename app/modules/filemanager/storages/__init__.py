from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Optional, List, Union, Dict

from app import schemas
from app.helper.storage import StorageHelper


class StorageBase(metaclass=ABCMeta):
    """
    存储基类
    """
    schema = None
    transtype = {}

    def __init__(self):
        self.storagehelper = StorageHelper()

    def generate_qrcode(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        pass

    def check_login(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        pass

    def get_config(self) -> Optional[schemas.StorageConf]:
        """
        获取配置
        """
        return self.storagehelper.get_storage(self.schema.value)

    def set_config(self, conf: dict):
        """
        设置配置
        """
        self.storagehelper.set_storage(self.schema.value, conf)

    def support_transtype(self) -> dict:
        """
        支持的整理方式
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        是否支持整理方式
        """
        return transtype in self.transtype

    @abstractmethod
    def check(self) -> bool:
        """
        检查存储是否可用
        """
        pass

    @abstractmethod
    def list(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        """
        pass

    @abstractmethod
    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        pass

    @abstractmethod
    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        pass

    @abstractmethod
    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        pass

    @abstractmethod
    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        pass

    @abstractmethod
    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        pass

    @abstractmethod
    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Path:
        """
        下载文件，保存到本地，返回本地临时文件地址
        """
        pass

    @abstractmethod
    def upload(self, fileitem: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        pass

    @abstractmethod
    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        pass

    @abstractmethod
    def copy(self, fileitem: schemas.FileItem, target: Union[schemas.FileItem, Path]) -> bool:
        """
        复制文件
        """
        pass

    @abstractmethod
    def move(self, fileitem: schemas.FileItem, target: Union[schemas.FileItem, Path]) -> bool:
        """
        移动文件
        """
        pass

    @abstractmethod
    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        pass

    @abstractmethod
    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        pass

    @abstractmethod
    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        pass

    def snapshot(self, path: Path) -> Dict[str, float]:
        """
        快照文件系统，输出所有层级文件信息（不含目录）
        """
        files_info = {}

        def __snapshot_file(_fileitm: schemas.FileItem):
            """
            递归获取文件信息
            """
            if _fileitm.type == "dir":
                for sub_file in self.list(_fileitm):
                    __snapshot_file(sub_file)
            else:
                files_info[_fileitm.path] = _fileitm.size

        fileitem = self.get_item(path)
        if not fileitem:
            return {}

        __snapshot_file(fileitem)

        return files_info
