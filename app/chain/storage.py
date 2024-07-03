from pathlib import Path
from typing import Optional, Tuple, List, Dict

from app import schemas
from app.chain import ChainBase


class StorageChain(ChainBase):
    """
    存储处理链
    """

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        return self.run_module("generate_qrcode",)

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        登录确认
        """
        return self.run_module("check_login",)

    def list_files(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        查询当前目录下所有目录和文件
        """
        return self.run_module("list_files", fileitem=fileitem)

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        return self.run_module("create_folder", fileitem=fileitem, name=name)

    def download_file(self, fileitem: schemas.FileItem, path: str) -> Optional[bool]:
        """
        下载文件
        """
        return self.run_module("download_file", fileitem=fileitem, path=path)

    def upload_file(self, fileitem: schemas.FileItem, path: Path) -> Optional[bool]:
        """
        上传文件
        """
        return self.run_module("upload_file", fileitem=fileitem, path=path)

    def delete_file(self, fileitem: schemas.FileItem) -> Optional[bool]:
        """
        删除文件或目录
        """
        return self.run_module("delete_file", fileitem=fileitem)

    def rename_file(self, fileitem: schemas.FileItem, name: str) -> Optional[bool]:
        """
        重命名文件或目录
        """
        return self.run_module("rename_file", fileitem=fileitem, name=name)

    def snapshot_storage(self, fileitem: schemas.FileItem) -> Optional[Dict]:
        """
        快照存储
        """
        return self.run_module("snapshot_storage", fileitem=fileitem)
