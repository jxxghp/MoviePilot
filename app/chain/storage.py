from pathlib import Path
from typing import Optional, Tuple, List, Dict

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType


class StorageChain(ChainBase):
    """
    存储处理链
    """

    def save_config(self, storage: str, conf: dict) -> None:
        """
        保存存储配置
        """
        self.run_module("save_config", storage=storage, conf=conf)

    def generate_qrcode(self, storage: str) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        return self.run_module("generate_qrcode", storage=storage)

    def check_login(self, storage: str, **kwargs) -> Optional[Tuple[dict, str]]:
        """
        登录确认
        """
        return self.run_module("check_login", storage=storage, **kwargs)

    def list_files(self, fileitem: schemas.FileItem, recursion: bool = False) -> Optional[List[schemas.FileItem]]:
        """
        查询当前目录下所有目录和文件
        """
        return self.run_module("list_files", fileitem=fileitem, recursion=recursion)

    def any_files(self, fileitem: schemas.FileItem, extensions: list = None) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        return self.run_module("any_files", fileitem=fileitem, extensions=extensions)

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        return self.run_module("create_folder", fileitem=fileitem, name=name)

    def download_file(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件
        :param fileitem: 文件项
        :param path: 本地保存路径
        """
        return self.run_module("download_file", fileitem=fileitem, path=path)

    def upload_file(self, fileitem: schemas.FileItem, path: Path) -> Optional[bool]:
        """
        上传文件
        :param fileitem: 保存目录项
        :param path: 本地文件路径
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

    def get_file_item(self, storage: str, path: Path) -> Optional[schemas.FileItem]:
        """
        根据路径获取文件项
        """
        return self.run_module("get_file_item", storage=storage, path=path)

    def get_parent_item(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取上级目录项
        """
        return self.run_module("get_parent_item", fileitem=fileitem)

    def snapshot_storage(self, storage: str, path: Path) -> Optional[Dict[str, float]]:
        """
        快照存储
        """
        return self.run_module("snapshot_storage", storage=storage, path=path)

    def storage_usage(self, storage: str) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        return self.run_module("storage_usage", storage=storage)

    def support_transtype(self, storage: str) -> Optional[str]:
        """
        获取支持的整理方式
        """
        return self.run_module("support_transtype", storage=storage)

    def delete_media_file(self, fileitem: schemas.FileItem, mtype: MediaType = None) -> bool:
        """
        删除媒体文件，以及不含媒体文件的目录
        """
        if fileitem.path == "/" or len(Path(fileitem.path).parts) <= 2:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 根目录或一级目录不允许删除")
            return False
        logger.warn(f"正在删除【{fileitem.storage}】{fileitem.path}")
        state = self.delete_file(fileitem)
        if not state:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
            return False
        if fileitem.type == "dir":
            # 本身是目录不处理父目录
            return True
        # 上级目录
        if mtype and mtype == MediaType.TV:
            dir_item = self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path).parent.parent)
        else:
            dir_item = self.get_parent_item(fileitem)
        if dir_item and len(Path(dir_item.path).parts) > 2:
            # 不存在其他媒体文件，删除空目录
            exts = settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT
            if self.any_files(dir_item, extensions=exts) is False:
                logger.warn(f"【{dir_item.storage}】{dir_item.path} 不存在其它媒体文件，删除空目录")
                return self.delete_file(dir_item)

        # 存在媒体文件，返回文件删除状态
        return state
