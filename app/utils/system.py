import os
import platform
import re
import shutil
from pathlib import Path


class SystemUtils:

    @staticmethod
    def execute(cmd: str) -> str:
        """
        执行命令，获得返回结果
        """
        try:
            with os.popen(cmd) as p:
                return p.readline().strip()
        except Exception as err:
            print(str(err))
            return ""

    @staticmethod
    def is_docker() -> bool:
        return Path("/.dockerenv").exists()

    @staticmethod
    def is_synology() -> bool:
        if SystemUtils.is_windows():
            return False
        return True if "synology" in SystemUtils.execute('uname -a') else False

    @staticmethod
    def is_windows() -> bool:
        return True if os.name == "nt" else False

    @staticmethod
    def is_macos() -> bool:
        return True if platform.system() == 'Darwin' else False

    @staticmethod
    def copy(src: Path, dest: Path):
        """
        复制
        """
        try:
            shutil.copy2(src, dest)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def move(src: Path, dest: Path):
        """
        移动
        """
        try:
            shutil.move(src.with_name(dest.name), dest)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def link(src: Path, dest: Path):
        """
        硬链接
        """
        try:
            os.link(src, dest)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def softlink(src: Path, dest: Path):
        """
        软链接
        """
        try:
            os.symlink(src, dest)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def list_files_with_extensions(directory: Path, extensions: list) -> list:
        files = []
        pattern = r".*\.(" + "|".join(extensions) + ")$"

        # 遍历目录及子目录
        for path in directory.glob('**/*'):
            if path.is_file() and re.match(pattern, str(path), re.IGNORECASE):
                files.append(path)

        return files
