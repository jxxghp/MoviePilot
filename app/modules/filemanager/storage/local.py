import shutil
from pathlib import Path
from typing import Optional, List

from app import schemas
from app.log import logger
from app.modules.filemanager.storage import StorageBase
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

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        return True

    def list(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
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
            logger.warn(f"目录不存在：{path}")
            return []

        # 如果是文件
        if path_obj.is_file():
            ret_items.append(schemas.FileItem(
                storage=self.schema.value,
                type="file",
                path=str(path_obj).replace("\\", "/"),
                name=path_obj.name,
                basename=path_obj.stem,
                extension=path_obj.suffix[1:],
                size=path_obj.stat().st_size,
                modify_time=path_obj.stat().st_mtime,
            ))
            return ret_items

        # 扁历所有目录
        for item in SystemUtils.list_sub_directory(path_obj):
            ret_items.append(schemas.FileItem(
                storage=self.schema.value,
                type="dir",
                path=str(item).replace("\\", "/") + "/",
                name=item.name,
                basename=item.stem,
                modify_time=item.stat().st_mtime,
            ))

        # 遍历所有文件，不含子目录
        for item in SystemUtils.list_sub_all(path_obj):
            ret_items.append(schemas.FileItem(
                storage=self.schema.value,
                type="file",
                path=str(item).replace("\\", "/"),
                name=item.name,
                basename=item.stem,
                extension=item.suffix[1:],
                size=item.stat().st_size,
                modify_time=item.stat().st_mtime,
            ))
        return ret_items

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path) / name
        if not path_obj.exists():
            path_obj.mkdir(parents=True, exist_ok=True)
        return schemas.FileItem(
            storage=self.schema.value,
            type="dir",
            path=str(path_obj).replace("\\", "/") + "/",
            name=name,
            basename=name,
            modify_time=path_obj.stat().st_mtime,
        )

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录
        """
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return schemas.FileItem(
            storage=self.schema.value,
            type="dir",
            path=str(path).replace("\\", "/") + "/",
            name=path.name,
            basename=path.stem,
            modify_time=path.stat().st_mtime,
        )

    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        path_obj = Path(fileitm.path)
        return schemas.FileItem(
            storage=self.schema.value,
            type="file",
            path=str(path_obj).replace("\\", "/"),
            name=path_obj.name,
            basename=path_obj.stem,
            extension=path_obj.suffix[1:],
            size=path_obj.stat().st_size,
            modify_time=path_obj.stat().st_mtime,
        )

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        if not fileitem.path:
            return False
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return False
        if path_obj.is_file():
            path_obj.unlink()
        else:
            shutil.rmtree(path_obj, ignore_errors=True)
        return True

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return False
        path_obj.rename(path_obj.parent / name)

    def download(self, fileitem: schemas.FileItem, path: Path) -> bool:
        """
        下载文件
        """
        return False

    def upload(self, fileitem: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        filepath = Path(fileitem.path)
        if not filepath.exists():
            logger.warn(f"文件不存在：{filepath}")
            return None
        if not path.exists():
            filepath.rename(path)
        if path.exists():
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

    def copy(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        复制文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.copy(file_path, target_file)
        if code != 0:
            logger.error(f"复制文件失败：{message}")
            return False
        return True

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.link(file_path, target_file)
        if code != 0:
            logger.error(f"硬链接文件失败：{message}")
            return False
        return True

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.copy(file_path, target_file)
        if code != 0:
            logger.error(f"软链接文件失败：{message}")
            return False
        return True

    def move(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        移动文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.move(file_path, target_file)
        if code != 0:
            logger.error(f"移动文件失败：{message}")
            return False
        return True
