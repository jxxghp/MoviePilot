import subprocess
from pathlib import Path
from typing import Optional, List

from app import schemas
from app.log import logger
from app.modules.filemanager.storage import StorageBase
from app.schemas.types import StorageSchema
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

    @staticmethod
    def __get_hidden_shell():
        if SystemUtils.is_windows():
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    def check(self) -> bool:
        pass

    def list(self, fileitm: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        pass

    def create_folder(self, fileitm: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        pass

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录
        """
        pass

    def delete(self, fileitm: schemas.FileItem) -> bool:
        pass

    def rename(self, fileitm: schemas.FileItem, name: str) -> bool:
        pass

    def download(self, fileitm: schemas.FileItem, path: Path) -> bool:
        pass

    def upload(self, fileitm: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        pass

    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        pass

    def move(self, fileitm: schemas.FileItem, target_file: schemas.FileItem) -> bool:
        """
        移动文件，target_file格式：rclone:path
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'moveto',
                    fileitm.path,
                    f'{target_file}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"移动文件失败：{err}")
        return False

    def copy(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        """
        复制文件，target_file格式：rclone:path
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'copyto',
                    fileitm.path,
                    f'{target_file}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"复制文件失败：{err}")
        return False

    def link(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass
