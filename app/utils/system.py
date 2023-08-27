import datetime
import os
import platform
import re
import shutil
from pathlib import Path
from typing import List, Union, Tuple
import psutil
from app import schemas


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
    def copy(src: Path, dest: Path) -> Tuple[int, str]:
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
    def move(src: Path, dest: Path) -> Tuple[int, str]:
        """
        移动
        """
        try:
            temp = src.replace(src.parent / dest.name)
            shutil.move(temp, dest)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def link(src: Path, dest: Path) -> Tuple[int, str]:
        """
        硬链接
        """
        try:
            dest.hardlink_to(src)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def softlink(src: Path, dest: Path) -> Tuple[int, str]:
        """
        软链接
        """
        try:
            dest.symlink_to(src)
            return 0, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def list_files(directory: Path, extensions: list, min_filesize: int = 0) -> List[Path]:
        """
        获取目录下所有指定扩展名的文件（包括子目录）
        """
        if not directory.exists():
            return []

        if directory.is_file():
            return [directory]

        files = []
        pattern = r".*(" + "|".join(extensions) + ")$"

        # 遍历目录及子目录
        for path in directory.rglob('**/*'):
            if path.is_file() \
                    and re.match(pattern, path.name, re.IGNORECASE) \
                    and path.stat().st_size >= min_filesize * 1024 * 1024:
                files.append(path)

        return files

    @staticmethod
    def list_sub_files(directory: Path, extensions: list) -> List[Path]:
        """
        列出当前目录下的所有指定扩展名的文件(不包括子目录)
        """
        if not directory.exists():
            return []

        if directory.is_file():
            return [directory]

        files = []
        pattern = r".*(" + "|".join(extensions) + ")$"

        # 遍历目录
        for path in directory.iterdir():
            if path.is_file() and re.match(pattern, path.name, re.IGNORECASE):
                files.append(path)

        return files

    @staticmethod
    def list_sub_directory(directory: Path) -> List[Path]:
        """
        列出当前目录下的所有子目录（不递归）
        """
        if not directory.exists():
            return []

        if directory.is_file():
            return []

        dirs = []

        # 遍历目录
        for path in directory.iterdir():
            if path.is_dir():
                dirs.append(path)

        return dirs

    @staticmethod
    def get_directory_size(path: Path) -> float:
        """
        计算目录的大小

        参数:
            directory_path (Path): 目录路径

        返回:
            int: 目录的大小（以字节为单位）
        """
        if not path or not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        total_size = 0
        for path in path.glob('**/*'):
            if path.is_file():
                total_size += path.stat().st_size

        return total_size

    @staticmethod
    def space_usage(dir_list: Union[Path, List[Path]]) -> Tuple[float, float]:
        """
        计算多个目录的总可用空间/剩余空间（单位：Byte），并去除重复磁盘
        """
        if not dir_list:
            return 0.0, 0.0
        if not isinstance(dir_list, list):
            dir_list = [dir_list]
        # 存储不重复的磁盘
        disk_set = set()
        # 存储总剩余空间
        total_free_space = 0.0
        # 存储总空间
        total_space = 0.0
        for dir_path in dir_list:
            if not dir_path:
                continue
            if not dir_path.exists():
                continue
            # 获取目录所在磁盘
            if os.name == "nt":
                disk = dir_path.drive
            else:
                disk = os.stat(dir_path).st_dev
            # 如果磁盘未出现过，则计算其剩余空间并加入总剩余空间中
            if disk not in disk_set:
                disk_set.add(disk)
                total_space += SystemUtils.total_space(dir_path)
                total_free_space += SystemUtils.free_space(dir_path)
        return total_space, total_free_space

    @staticmethod
    def free_space(path: Path) -> float:
        """
        获取指定路径的剩余空间（单位：Byte）
        """
        if not os.path.exists(path):
            return 0.0
        return psutil.disk_usage(str(path)).free

    @staticmethod
    def total_space(path: Path) -> float:
        """
        获取指定路径的总空间（单位：Byte）
        """
        if not os.path.exists(path):
            return 0.0
        return psutil.disk_usage(str(path)).total

    @staticmethod
    def processes() -> List[schemas.ProcessInfo]:
        """
        获取所有进程
        """
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'create_time', 'memory_info', 'status']):
            try:
                if proc.status() != psutil.STATUS_ZOMBIE:
                    runtime = datetime.datetime.now() - datetime.datetime.fromtimestamp(
                        int(getattr(proc, 'create_time', 0)()))
                    mem_info = getattr(proc, 'memory_info', None)()
                    if mem_info is not None:
                        mem_mb = round(mem_info.rss / (1024 * 1024), 1)
                        processes.append(schemas.ProcessInfo(
                            pid=proc.pid, name=proc.name(), run_time=runtime.seconds, memory=mem_mb
                        ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return processes

    @staticmethod
    def is_bluray_dir(dir_path: Path) -> bool:
        """
        判断是否为蓝光原盘目录
        """
        # 蓝光原盘目录必备的文件或文件夹
        required_files = ['BDMV', 'CERTIFICATE']
        # 检查目录下是否存在所需文件或文件夹
        for item in required_files:
            if (dir_path / item).exists():
                return True
        return False

    @staticmethod
    def get_windows_drives():
        """
        获取Windows所有盘符
        """
        vols = []
        for i in range(65, 91):
            vol = chr(i) + ':'
            if os.path.isdir(vol):
                vols.append(vol)
        return vols
