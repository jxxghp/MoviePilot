from pathlib import Path
from typing import Optional, Tuple, List, Dict

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.helper.directory import DirectoryHelper
from app.log import logger


class StorageChain(ChainBase):
    """
    存储处理链
    """

    def save_config(self, storage: str, conf: dict) -> None:
        """
        保存存储配置
        """
        self.run_module("save_config", storage=storage, conf=conf)

    def reset_config(self, storage: str) -> None:
        """
        重置存储配置
        """
        self.run_module("reset_config", storage=storage)

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

    def upload_file(self, fileitem: schemas.FileItem, path: Path,
                    new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 保存目录项
        :param path: 本地文件路径
        :param new_name: 新文件名
        """
        return self.run_module("upload_file", fileitem=fileitem, path=path, new_name=new_name)

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

    def exists(self, fileitem: schemas.FileItem) -> Optional[bool]:
        """
        判断文件或目录是否存在
        """
        return True if self.get_item(fileitem) else False

    def get_item(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        查询目录或文件
        """
        return self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path))

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

    def snapshot_storage(self, storage: str, path: Path,
                         last_snapshot_time: float = None, max_depth: int = 5) -> Optional[Dict[str, Dict]]:
        """
        快照存储
        :param storage: 存储类型
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        """
        return self.run_module("snapshot_storage", storage=storage, path=path,
                               last_snapshot_time=last_snapshot_time, max_depth=max_depth)

    def storage_usage(self, storage: str) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        return self.run_module("storage_usage", storage=storage)

    def support_transtype(self, storage: str) -> Optional[dict]:
        """
        获取支持的整理方式
        """
        return self.run_module("support_transtype", storage=storage)

    def delete_media_file(self, fileitem: schemas.FileItem, delete_self: bool = True) -> bool:
        """
        删除媒体文件，以及不含媒体文件的目录
        """

        def __is_bluray_dir(_fileitem: schemas.FileItem) -> bool:
            """
            检查是否蓝光目录
            """
            _dir_files = self.list_files(fileitem=_fileitem, recursion=False)
            if _dir_files:
                for _f in _dir_files:
                    if _f.type == "dir" and _f.name in ["BDMV", "CERTIFICATE"]:
                        return True
            return False

        media_exts = settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT
        fileitem_path = Path(fileitem.path) if fileitem.path else Path("")
        if len(fileitem_path.parts) <= 2:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 根目录或一级目录不允许删除")
            return False
        if fileitem.type == "dir":
            # 本身是目录
            if __is_bluray_dir(fileitem):
                logger.warn(f"正在删除蓝光原盘目录：【{fileitem.storage}】{fileitem.path}")
                if not self.delete_file(fileitem):
                    logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                    return False

        elif delete_self:
            # 本身是文件，需要删除文件
            logger.warn(f"正在删除文件【{fileitem.storage}】{fileitem.path}")
            if not self.delete_file(fileitem):
                logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                return False

        # 检查和删除上级空目录
        dir_item = fileitem if fileitem.type == "dir" else self.get_parent_item(fileitem)
        if not dir_item:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 上级目录不存在")
            return False

        # 查找操作文件项匹配的配置目录(资源目录、媒体库目录)
        associated_dir = max(
            (
                Path(p)
                for d in DirectoryHelper().get_dirs()
                for p in (d.download_path, d.library_path)
                if p and fileitem_path.is_relative_to(p)
            ),
            key=lambda path: len(path.parts),
            default=None,
        )

        while dir_item and len(Path(dir_item.path).parts) > 2:
            # 目录是资源目录、媒体库目录的上级，则不处理
            if associated_dir and associated_dir.is_relative_to(Path(dir_item.path)):
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 位于资源或媒体库目录结构中，不删除")
                break

            elif not associated_dir and self.list_files(dir_item, recursion=False):
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 不是空目录，不删除")
                break

            if self.any_files(dir_item, extensions=media_exts) is not False:
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 存在媒体文件，不删除")
                break

            # 删除空目录并继续处理父目录
            logger.warn(f"【{dir_item.storage}】{dir_item.path} 不存在其它媒体文件，正在删除空目录")
            if not self.delete_file(dir_item):
                logger.warn(f"【{dir_item.storage}】{dir_item.path} 删除失败")
                return False
            dir_item = self.get_parent_item(dir_item)

        return True
