from pathlib import Path
from typing import Optional, Tuple, List, Dict

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.helper.directory import DirectoryHelper
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

    def support_transtype(self, storage: str) -> Optional[dict]:
        """
        获取支持的整理方式
        """
        return self.run_module("support_transtype", storage=storage)

    def delete_media_file(self, fileitem: schemas.FileItem,
                          mtype: MediaType = None, delete_self: bool = True) -> bool:
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
        if fileitem.path == "/" or len(Path(fileitem.path).parts) <= 2:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 根目录或一级目录不允许删除")
            return False
        if fileitem.type == "dir":
            # 本身是目录
            if __is_bluray_dir(fileitem):
                logger.warn(f"正在删除蓝光原盘目录：【{fileitem.storage}】{fileitem.path}")
                if not self.delete_file(fileitem):
                    logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                    return False
            elif self.any_files(fileitem, extensions=media_exts) is False:
                logger.warn(f"【{fileitem.storage}】{fileitem.path} 不存在其它媒体文件，正在删除空目录")
                if not self.delete_file(fileitem):
                    logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                    return False
            # 不处理父目录
            return True
        elif delete_self:
            # 本身是文件，需要删除文件
            logger.warn(f"正在删除文件【{fileitem.storage}】{fileitem.path}")
            if not self.delete_file(fileitem):
                logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                return False

        if mtype:
            # 重命名格式
            rename_format = settings.TV_RENAME_FORMAT \
                if mtype == MediaType.TV else settings.MOVIE_RENAME_FORMAT
            # 计算重命名中的文件夹层数
            rename_format_level = len(rename_format.split("/")) - 1
            if rename_format_level < 1:
                return True
            # 处理媒体文件根目录
            dir_item = self.get_file_item(storage=fileitem.storage,
                                          path=Path(fileitem.path).parents[rename_format_level - 1])
        else:
            # 处理上级目录
            dir_item = self.get_parent_item(fileitem)

        # 检查和删除上级目录
        if dir_item and len(Path(dir_item.path).parts) > 2:
            # 如何目录是所有下载目录、媒体库目录的上级，则不处理
            for d in DirectoryHelper().get_dirs():
                if d.download_path and Path(d.download_path).is_relative_to(Path(dir_item.path)):
                    logger.debug(f"【{dir_item.storage}】{dir_item.path} 是下载目录本级或上级目录，不删除")
                    return True
                if d.library_path and Path(d.library_path).is_relative_to(Path(dir_item.path)):
                    logger.debug(f"【{dir_item.storage}】{dir_item.path} 是媒体库目录本级或上级目录，不删除")
                    return True
            # 不存在其他媒体文件，删除空目录
            if self.any_files(dir_item, extensions=media_exts) is False:
                logger.warn(f"【{dir_item.storage}】{dir_item.path} 不存在其它媒体文件，正在删除空目录")
                if not self.delete_file(dir_item):
                    logger.warn(f"【{dir_item.storage}】{dir_item.path} 删除失败")
                    return False

        return True
