import copy
import json
import subprocess
from pathlib import Path
from typing import Optional, List

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


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

    def init_storage(self):
        """
        初始化
        """
        pass

    def set_config(self, conf: dict):
        """
        设置配置
        """
        super().set_config(conf)
        filepath = conf.get("filepath")
        if not filepath:
            logger.warn("Rclone保存配置失败：未设置配置文件路径")
        logger.info(f"Rclone配置写入文件：{filepath}")
        path = Path(filepath)
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
        path.write_text(conf.get('content'), encoding='utf-8')

    @staticmethod
    def __get_hidden_shell():
        if SystemUtils.is_windows():
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    def __get_fileitem(self, path: Path):
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

    def __get_rcloneitem(self, item: dict, parent: str = "/") -> schemas.FileItem:
        """
        获取rclone文件项
        """
        if not item:
            return schemas.FileItem()
        if item.get("IsDir"):
            return schemas.FileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{parent}{item.get('Name')}" + "/",
                name=item.get("Name"),
                basename=item.get("Name"),
                modify_time=StringUtils.str_to_timestamp(item.get("ModTime"))
            )
        else:
            return schemas.FileItem(
                storage=self.schema.value,
                type="file",
                path=f"{parent}{item.get('Name')}",
                name=item.get("Name"),
                basename=Path(item.get("Name")).stem,
                extension=Path(item.get("Name")).suffix[1:],
                size=item.get("Size"),
                modify_time=StringUtils.str_to_timestamp(item.get("ModTime"))
            )

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        try:
            retcode = subprocess.run(
                ['rclone', 'lsf', 'MP:'],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"rclone存储检查失败：{err}")
        return False

    def list(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        """
        if fileitem.type == "file":
            return [fileitem]
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{fileitem.path}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return [self.__get_rcloneitem(item, parent=fileitem.path) for item in items]
        except Exception as err:
            logger.error(f"rclone浏览文件失败：{err}")
        return []

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'mkdir',
                    f'MP:{fileitem.path}/{name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                ret_fileitem = copy.deepcopy(fileitem)
                ret_fileitem.path = f"{fileitem.path}/{name}/"
                ret_fileitem.name = name
                return ret_fileitem
        except Exception as err:
            logger.error(f"rclone创建目录失败：{err}")
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_file in self.list(_fileitem):
                if sub_file.type != "dir":
                    continue
                if sub_file.name == _name:
                    return sub_file
            return None

        # 逐级查找和创建目录
        fileitem = schemas.FileItem(path="/")
        for part in path.parts:
            if part == "/":
                continue
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"rclone创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{path}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return self.__get_rcloneitem(items[0])
        except Exception as err:
            logger.error(f"rclone获取文件失败：{err}")
        return None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'deletefile',
                    f'MP:{fileitem.path}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"rclone删除文件失败：{err}")
        return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'moveto',
                    f'MP:{fileitem.path}',
                    f'MP:{Path(fileitem.path).parent}/{name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"rclone重命名文件失败：{err}")
        return False

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件
        """
        path = (path or settings.TEMP_PATH) / fileitem.name
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'copyto',
                    f'MP:{fileitem.path}',
                    f'{path}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return path
        except Exception as err:
            logger.error(f"rclone复制文件失败：{err}")
        return None

    def upload(self, fileitem: schemas.FileItem, path: Path, new_name: str = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        try:
            new_path = Path(fileitem.path) / (new_name or path.name)
            retcode = subprocess.run(
                [
                    'rclone', 'copyto',
                    str(path),
                    f'MP:{new_path}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return self.__get_fileitem(new_path)
        except Exception as err:
            logger.error(f"rclone上传文件失败：{err}")
        return None

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{fileitem.path}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return self.__get_rcloneitem(items[0])
        except Exception as err:
            logger.error(f"rclone获取文件详情失败：{err}")
        return None

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'moveto',
                    f'MP:{fileitem.path}',
                    f'MP:{path / new_name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"rclone移动文件失败：{err}")
        return False

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'copyto',
                    f'MP:{fileitem.path}',
                    f'MP:{path / new_name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"rclone复制文件失败：{err}")
        return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        conf = self.get_config()
        if not conf:
            return None
        file_path = conf.config.get("filepath")
        if not file_path or not Path(file_path).exists():
            return None
        # 读取rclone文件，检查是否有[MP]节点配置
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if not lines:
                return None
            if not any("[MP]" in line.strip() for line in lines):
                return None
        try:
            ret = subprocess.run(
                [
                    'rclone', 'about',
                    'MP:/', '--json'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return schemas.StorageUsage(
                    total=items.get("total"),
                    available=items.get("free")
                )
        except Exception as err:
            logger.error(f"rclone获取存储使用情况失败：{err}")
        return None
