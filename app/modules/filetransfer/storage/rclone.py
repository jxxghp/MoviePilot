from pathlib import Path
from typing import Optional, Any, List

from app import schemas
from app.modules.filetransfer.storage import StorageBase
from app.schemas.types import StorageSchema


class Rclone(StorageBase):
    """
    rclone相关操作
    """

    # 存储类型
    schema = StorageSchema.Rclone
    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制"
    }

    def check(self) -> bool:
        pass

    def list(self, fileitm: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        pass

    def create_folder(self, fileitm: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        pass

    def delete(self, fileitm: schemas.FileItem) -> bool:
        pass

    def rename(self, fileitm: schemas.FileItem, name: str) -> bool:
        pass

    def download(self, fileitm: schemas.FileItem) -> Any:
        pass

    def upload(self, fileitm: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        pass

    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        pass

    def move(self, fileitm: schemas.FileItem, target_dir: schemas.FileItem) -> bool:
        pass

    def copy(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def link(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitm: schemas.FileItem, target_file: schemas.FileItem) -> bool:
        pass
