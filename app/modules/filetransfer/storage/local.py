import shutil
from pathlib import Path
from typing import Optional, List

from starlette.responses import FileResponse, Response

from app import schemas
from app.log import logger
from app.modules.filetransfer.storage import StorageBase
from app.utils.system import SystemUtils


class LocalStorage(StorageBase):
    """
    本地文件操作
    """

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
                type="dir",
                path=str(item).replace("\\", "/") + "/",
                name=item.name,
                basename=item.stem,
                modify_time=item.stat().st_mtime,
            ))

        # 遍历所有文件，不含子目录
        for item in SystemUtils.list_sub_all(path_obj):
            ret_items.append(schemas.FileItem(
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
        if path_obj.exists():
            return None
        path_obj.mkdir(parents=True, exist_ok=True)
        return schemas.FileItem(
            type="dir",
            path=str(path_obj).replace("\\", "/") + "/",
            name=name,
            basename=name,
            modify_time=path_obj.stat().st_mtime,
        )

    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        path_obj = Path(fileitm.path)
        return schemas.FileItem(
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

    def download(self, fileitem: schemas.FileItem) -> Optional[Response]:
        """
        下载文件
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return None
        if path_obj.is_file():
            # 做为文件流式下载
            return FileResponse(path_obj)
        else:
            # 做为压缩包下载
            shutil.make_archive(base_name=path_obj.stem, format="zip", root_dir=path_obj)
            reponse = Response(content=path_obj.read_bytes(), media_type="application/zip")
            # 删除压缩包
            Path(f"{path_obj.stem}.zip").unlink()
            return reponse

    def move(self, fileitem: schemas.FileItem, target_dir: schemas.FileItem) -> bool:
        """
        移动文件
        """
        if not fileitem.path or not target_dir.path:
            return False
        path_obj = Path(fileitem.path)
        target_obj = Path(target_dir.path)
        if not path_obj.exists() or not target_obj.exists():
            return False
        path_obj.rename(target_obj / path_obj.name)
        return True

    def upload(self, fileitem: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return None
        shutil.copy(path, path_obj / path.name)
        return schemas.FileItem(
            type="file",
            path=str(path_obj / path.name).replace("\\", "/"),
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:],
            size=path.stat().st_size,
            modify_time=path.stat().st_mtime,
        )
