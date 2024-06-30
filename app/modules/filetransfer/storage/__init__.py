from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Optional, List, Any

from app import schemas


class StorageBase(metaclass=ABCMeta):
    """
    存储基类
    """
    
    @abstractmethod
    def check(self) -> bool:
        """
        检查存储是否可用
        """
        pass
    
    @abstractmethod
    def list(self, fileitm: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        """
        pass

    @abstractmethod
    def create_folder(self, fileitm: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        pass

    @abstractmethod
    def delete(self, fileitm: schemas.FileItem) -> bool:
        """
        删除文件
        """
        pass

    @abstractmethod
    def rename(self, fileitm: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        pass

    @abstractmethod
    def download(self, fileitm: schemas.FileItem) -> Any:
        """
        下载链接
        """
        pass

    @abstractmethod
    def move(self, fileitm: schemas.FileItem, target_dir: schemas.FileItem) -> bool:
        """
        移动文件
        """
        pass

    @abstractmethod
    def upload(self, fileitm: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        pass
    
    @abstractmethod
    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        pass
    