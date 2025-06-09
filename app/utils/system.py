import datetime
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from glob import glob
from pathlib import Path
from typing import List, Optional, Tuple, Union

import psutil

from app import schemas


class SystemUtils:
    """
    系统工具类，提供系统相关的操作和信息获取方法。
    """

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
    def execute_with_subprocess(pip_command: list) -> Tuple[bool, str]:
        """
        执行命令并捕获标准输出和错误输出，记录日志。

        :param pip_command: 要执行的命令，以列表形式提供
        :return: (命令是否成功, 输出信息或错误信息)
        """
        try:
            # 使用 subprocess.run 捕获标准输出和标准错误
            result = subprocess.run(pip_command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # 合并 stdout 和 stderr
            output = result.stdout + result.stderr
            return True, output
        except subprocess.CalledProcessError as e:
            error_message = f"命令：{' '.join(pip_command)}，执行失败，错误信息：{e.stderr.strip()}"
            return False, error_message
        except Exception as e:
            error_message = f"未知错误，命令：{' '.join(pip_command)}，错误：{str(e)}"
            return False, error_message

    @staticmethod
    def is_docker() -> bool:
        """
        判断是否为Docker环境
        """
        return Path("/.dockerenv").exists()

    @staticmethod
    def is_synology() -> bool:
        """
        判断是否为群晖系统
        """
        if SystemUtils.is_windows():
            return False
        return True if "synology" in SystemUtils.execute('uname -a') else False

    @staticmethod
    def is_windows() -> bool:
        """
        判断是否为Windows系统
        """
        return True if os.name == "nt" else False

    @staticmethod
    def is_frozen() -> bool:
        """
        判断是否为冻结的二进制文件
        """
        return True if getattr(sys, 'frozen', False) else False

    @staticmethod
    def is_macos() -> bool:
        """
        判断是否为MacOS系统
        """
        return True if platform.system() == 'Darwin' else False

    @staticmethod
    def is_aarch64() -> bool:
        """
        判断是否为ARM64架构
        """
        return True if platform.machine() == 'aarch64' else False

    @staticmethod
    def platform() -> str:
        """
        获取系统平台
        """
        if SystemUtils.is_windows():
            return "Windows"
        elif SystemUtils.is_macos():
            return "MacOS"
        elif SystemUtils.is_aarch64():
            return "Arm64"
        else:
            return "Linux"

    @staticmethod
    def copy(src: Path, dest: Path) -> Tuple[int, str]:
        """
        复制
        """
        try:
            shutil.copy2(src, dest)
            return 0, ""
        except Exception as err:
            return -1, str(err)

    @staticmethod
    def move(src: Path, dest: Path) -> Tuple[int, str]:
        """
        移动
        """
        try:
            # 当前目录改名
            temp = src.replace(src.parent / dest.name)
            # 移动到目标目录
            shutil.move(temp, dest)
            return 0, ""
        except Exception as err:
            return -1, str(err)

    @staticmethod
    def link(src: Path, dest: Path) -> Tuple[int, str]:
        """
        硬链接
        """
        try:
            # 准备目标路径，增加后缀 .mp
            tmp_path = dest.with_suffix(dest.suffix + ".mp")
            # 检查目标路径是否已存在，如果存在则先unlink
            if tmp_path.exists():
                tmp_path.unlink()
            tmp_path.hardlink_to(src)
            # 硬链接完成，移除 .mp 后缀
            shutil.move(tmp_path, dest)
            return 0, ""
        except Exception as err:
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
            return -1, str(err)

    @staticmethod
    def list_files(directory: Path, extensions: list = None,
                   min_filesize: int = 0, recursive: bool = True) -> List[Path]:
        """
        获取目录下所有指定扩展名的文件（包括子目录）
        :param directory: 指定的父目录
        :param extensions: 需要包含的扩展名列表，例如 ['mkv', 'mp4']
        :param min_filesize: 文件最低大小，单位 MB
        :param recursive: 是否递归查找，可选参数，默认 True
        :return: 文件 Path 列表
        """

        if not min_filesize:
            min_filesize = 0

        if not directory.exists():
            return []

        if directory.is_file():
            return [directory]

        if not min_filesize:
            min_filesize = 0

        files = []
        if extensions:
            pattern = r".*(" + "|".join(extensions) + ")$"
        else:
            pattern = r".*"

        # 遍历目录及子目录
        for matched_glob in glob('**', root_dir=directory, recursive=recursive, include_hidden=True):
            path = directory.joinpath(matched_glob)
            if path.is_file() \
                    and re.match(pattern, path.name, re.IGNORECASE) \
                    and path.stat().st_size >= min_filesize * 1024 * 1024:
                files.append(path)

        return files

    @staticmethod
    def exits_files(directory: Path, extensions: list, min_filesize: int = 0, recursive: bool = True) -> bool:
        """
        判断目录下是否存在指定扩展名的文件

        :param directory: 指定的父目录
        :param extensions: 需要包含的扩展名列表，例如 ['mkv', 'mp4']
        :param min_filesize: 文件最低大小，单位 MB
        :param recursive: 是否递归查找，可选参数，默认 True
        :return: True存在 False不存在
        """

        if not min_filesize:
            min_filesize = 0

        if not directory.exists():
            return False

        if directory.is_file():
            return True

        if not min_filesize:
            min_filesize = 0

        pattern = r".*(" + "|".join(extensions) + ")$"

        # 遍历目录及子目录
        for matched_glob in glob('**', root_dir=directory, recursive=recursive, include_hidden=True):
            path = directory.joinpath(matched_glob)
            if path.is_file() \
                    and re.match(pattern, path.name, re.IGNORECASE) \
                    and path.stat().st_size >= min_filesize * 1024 * 1024:
                return True

        return False

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
                if not SystemUtils.is_windows() and path.name.startswith("."):
                    continue
                if path.name == "@eaDir":
                    continue
                dirs.append(path)

        return dirs

    @staticmethod
    def list_sub_file(directory: Path) -> List[Path]:
        """
        列出当前目录下的所有子目录和文件（不递归）
        """
        if not directory.exists():
            return []

        if directory.is_file():
            return [directory]

        items = []

        # 遍历目录
        for path in directory.iterdir():
            if path.is_file():
                items.append(path)

        return items

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
        if not dir_path.is_dir():
            return False
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

    @staticmethod
    def cpu_usage():
        """
        获取CPU使用率
        """
        return psutil.cpu_percent()

    @staticmethod
    def memory_usage() -> List[int]:
        """
        获取当前程序的内存使用量和使用率
        """
        current_process = psutil.Process()
        process_memory = current_process.memory_info().rss
        system_memory = psutil.virtual_memory().total
        process_memory_percent = (process_memory / system_memory) * 100
        return [process_memory, int(process_memory_percent)]

    @staticmethod
    def is_hardlink(src: Path, dest: Path) -> bool:
        """
        判断是否为硬链接（可能无法支持宿主机挂载smb盘符映射docker的场景）
        """
        try:
            if not src.exists() or not dest.exists():
                return False
            if src.is_file():
                # 如果是文件，直接比较文件
                return src.samefile(dest)
            else:
                for src_file in src.glob("**/*"):
                    if src_file.is_dir():
                        continue
                    # 计算目标文件路径
                    relative_path = src_file.relative_to(src)
                    target_file = dest.joinpath(relative_path)
                    # 检查是否是硬链接
                    if not target_file.exists() or not src_file.samefile(target_file):
                        return False
                return True
        except Exception as e:
            print(f"Error occurred: {e}")
            return False

    @staticmethod
    def is_same_disk(src: Path, dest: Path) -> bool:
        """
        判断两个路径是否在同一磁盘
        """
        if not src.exists() or not dest.exists():
            return False
        if os.name == "nt":
            return src.drive == dest.drive
        return os.stat(src).st_dev == os.stat(dest).st_dev

    @staticmethod
    def get_config_path(config_dir: Optional[str] = None) -> Path:
        """
        获取配置路径
        """
        if not config_dir:
            config_dir = os.getenv("CONFIG_DIR")
        if config_dir:
            return Path(config_dir)
        if SystemUtils.is_docker():
            return Path("/config")
        elif SystemUtils.is_frozen():
            return Path(sys.executable).parent / "config"
        else:
            return Path(__file__).parents[2] / "config"

    @staticmethod
    def get_env_path() -> Path:
        """
        获取配置路径
        """
        return SystemUtils.get_config_path() / "app.env"

    @staticmethod
    def clear(temp_path: Path, days: int):
        """
        清理指定目录中指定天数前的文件，递归删除子文件及空文件夹
        """
        if not temp_path.exists():
            return
        # 遍历目录及子目录中的所有文件和文件夹
        for file in temp_path.rglob('*'):
            # 如果是文件并且符合时间条件，则删除
            if file.is_file() and (
                    datetime.datetime.now() - datetime.datetime.fromtimestamp(file.stat().st_mtime)).days > days:
                file.unlink()
        # 删除空的文件夹
        for folder in sorted(temp_path.rglob('*'), reverse=True):
            # 确保是空文件夹
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    @staticmethod
    def generate_user_unique_id():
        """
        根据优先级依次尝试生成稳定唯一ID：
        1. 文件系统唯一标识符。
        2. MAC 地址。
        3. 主机名。
        """

        def get_filesystem_unique_id():
            """
            获取文件系统的唯一标识符。
            使用根目录的设备号和 inode。
            """
            try:
                stat_info = os.stat("/")
                fs_id = f"{stat_info.st_dev}-{stat_info.st_ino}"
                return hashlib.sha256(fs_id.encode("utf-8")).hexdigest()
            except Exception as e:
                print(str(e))
                return None

        def get_mac_address_id():
            """
            获取设备的 MAC 地址并生成唯一标识符。
            """
            try:
                mac_address = uuid.getnode()
                if (mac_address >> 40) % 2:  # 检查是否是虚拟MAC地址
                    raise ValueError("MAC地址可能是虚拟地址")
                mac_str = f"{mac_address:012x}"
                return hashlib.sha256(mac_str.encode("utf-8")).hexdigest()
            except Exception as e:
                print(str(e))
                return None

        for method in [get_filesystem_unique_id, get_mac_address_id]:
            unique_id = method()
            if unique_id:
                return unique_id
        return None

    @staticmethod
    def set_system_modified():
        """
        设置系统已修改标志
        """
        try:
            if SystemUtils.is_docker():
                Path("/__moviepilot__").touch(exist_ok=True)
        except Exception as e:
            print(f"设置系统修改标志失败: {str(e)}")

    @staticmethod
    def is_system_reset() -> bool:
        """
        检查系统是否已被重置
        :return: 如果系统已重置，返回 True；否则返回 False
        """
        if SystemUtils.is_docker():
            return not Path("/__moviepilot__").exists()
        return False
