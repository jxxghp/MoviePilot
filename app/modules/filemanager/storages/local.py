import shutil
from pathlib import Path
from typing import Optional, List

from app import schemas
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.system import SystemUtils


class LocalStorage(StorageBase):
    """
    本地文件操作
    """

    # 存储类型
    schema = StorageSchema.Local
    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
        "link": "硬链接",
        "softlink": "软链接"
    }

    def init_storage(self):
        """
        初始化
        """
        pass

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        return True

    def __get_fileitem(self, path: Path) -> schemas.FileItem:
        """
        获取文件项
        """
        return schemas.FileItem(
            storage=self.schema.value,
            type="file",
            path=str(path).replace("\\", "/"),
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:],
            size=path.stat().st_size,
            modify_time=path.stat().st_mtime,
        )

    def __get_diritem(self, path: Path) -> schemas.FileItem:
        """
        获取目录项
        """
        return schemas.FileItem(
            storage=self.schema.value,
            type="dir",
            path=str(path).replace("\\", "/") + "/",
            name=path.name,
            basename=path.stem,
            modify_time=path.stat().st_mtime,
        )

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        浏览文件
        """
        # 返回结果
        ret_items = []
        path = fileitem.path
        if not fileitem.path or fileitem.path == "/":
            if SystemUtils.is_windows():
                partitions = SystemUtils.get_windows_drives() or ["C:/"]
                for partition in partitions:
                    ret_items.append(schemas.FileItem(
                        storage=self.schema.value,
                        type="dir",
                        path=partition + "/",
                        name=partition,
                        basename=partition
                    ))
                return ret_items
            else:
                path = "/"
        else:
            if SystemUtils.is_windows():
                path = path.lstrip("/")
            elif not path.startswith("/"):
                path = "/" + path

        # 遍历目录
        path_obj = Path(path)
        if not path_obj.exists():
            logger.warn(f"【local】目录不存在：{path}")
            return []

        # 如果是文件
        if path_obj.is_file():
            ret_items.append(self.__get_fileitem(path_obj))
            return ret_items

        # 扁历所有目录
        for item in SystemUtils.list_sub_directory(path_obj):
            ret_items.append(self.__get_diritem(item))

        # 遍历所有文件，不含子目录
        for item in SystemUtils.list_sub_file(path_obj):
            ret_items.append(self.__get_fileitem(item))
        return ret_items

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path) / name
        if not path_obj.exists():
            path_obj.mkdir(parents=True)
        return self.__get_diritem(path_obj)

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录
        """
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return self.__get_diritem(path)

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        if not path.exists():
            return None
        if path.is_file():
            return self.__get_fileitem(path)
        return self.__get_diritem(path)

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return None
        return self.__get_fileitem(path_obj)

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        if not fileitem.path:
            return False
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return True
        try:
            if path_obj.is_file():
                path_obj.unlink()
            else:
                shutil.rmtree(path_obj, ignore_errors=True)
        except Exception as e:
            logger.error(f"【local】删除文件失败：{e}")
            return False
        return True

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return False
        try:
            path_obj.rename(path_obj.parent / name)
        except Exception as e:
            logger.error(f"【local】重命名文件失败：{e}")
            return False
        return True

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件
        """
        return Path(fileitem.path)

    def upload(self, fileitem: schemas.FileItem, path: Path, new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        """
        dir_path = Path(fileitem.path)
        target_path = dir_path / (new_name or path.name)
        code, message = SystemUtils.move(path, target_path)
        if code != 0:
            logger.error(f"【local】移动文件失败：{message}")
            return None
        return self.get_item(target_path)

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.link(file_path, target_file)
        if code != 0:
            logger.error(f"【local】硬链接文件失败：{message}")
            return False
        return True

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.softlink(file_path, target_file)
        if code != 0:
            logger.error(f"【local】软链接文件失败：{message}")
            return False
        return True

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.copy(file_path, path / new_name)
        if code != 0:
            logger.error(f"【local】复制文件失败：{message}")
            return False
        return True

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.move(file_path, path / new_name)
        if code != 0:
            logger.error(f"【local】移动文件失败：{message}")
            return False
        return True

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        library_dirs = DirectoryHelper().get_local_library_dirs()
        total_storage, free_storage = SystemUtils.space_usage([Path(d.library_path) for d in library_dirs])
        return schemas.StorageUsage(
            total=total_storage,
            available=free_storage
        )
