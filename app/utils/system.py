import datetime
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Union, Tuple

import docker
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
            print(str(err))
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
            print(str(err))
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
    def rclone_move(src: Path, dest: Path):
        """
        Rclone移动
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'moveto',
                    str(src),
                    f'MP:{dest}'
                ],
                startupinfo=SystemUtils.__get_hidden_shell()
            ).returncode
            return retcode, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def rclone_copy(src: Path, dest: Path):
        """
        Rclone复制
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'copyto',
                    str(src),
                    f'MP:{dest}'
                ],
                startupinfo=SystemUtils.__get_hidden_shell()
            ).returncode
            return retcode, ""
        except Exception as err:
            print(str(err))
            return -1, str(err)

    @staticmethod
    def __get_hidden_shell():
        if SystemUtils.is_windows():
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    @staticmethod
    def list_files(directory: Path, extensions: list, min_filesize: int = 0) -> List[Path]:
        """
        获取目录下所有指定扩展名的文件（包括子目录）
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
        pattern = r".*(" + "|".join(extensions) + ")$"

        # 遍历目录及子目录
        for path in directory.rglob('**/*'):
            if path.is_file() \
                    and re.match(pattern, path.name, re.IGNORECASE) \
                    and path.stat().st_size >= min_filesize * 1024 * 1024:
                files.append(path)

        return files

    @staticmethod
    def exits_files(directory: Path, extensions: list, min_filesize: int = 0) -> bool:
        """
        判断目录下是否存在指定扩展名的文件
        :return True存在 False不存在
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
        for path in directory.rglob('**/*'):
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
                dirs.append(path)

        return dirs

    @staticmethod
    def list_sub_all(directory: Path) -> List[Path]:
        """
        列出当前目录下的所有子目录和文件（不递归）
        """
        if not directory.exists():
            return []

        if directory.is_file():
            return []

        items = []

        # 遍历目录
        for path in directory.iterdir():
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
        获取内存使用量和使用率
        """
        return [psutil.virtual_memory().used, int(psutil.virtual_memory().percent)]

    @staticmethod
    def can_restart() -> bool:
        """
        判断是否可以内部重启
        """
        return Path("/var/run/docker.sock").exists()

    @staticmethod
    def restart() -> Tuple[bool, str]:
        """
        执行Docker重启操作
        """
        if not SystemUtils.is_docker():
            return False, "非Docker环境，无法重启！"
        try:
            # 创建 Docker 客户端
            client = docker.DockerClient(base_url='tcp://127.0.0.1:38379')
            # 获取当前容器的 ID
            container_id = None
            with open('/proc/self/mountinfo', 'r') as f:
                data = f.read()
                index_resolv_conf = data.find("resolv.conf")
                if index_resolv_conf != -1:
                    index_second_slash = data.rfind("/", 0, index_resolv_conf)
                    index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                    container_id = data[index_first_slash:index_second_slash]
                    if len(container_id) < 20:
                        index_resolv_conf = data.find("/sys/fs/cgroup/devices")
                        if index_resolv_conf != -1:
                            index_second_slash = data.rfind(" ", 0, index_resolv_conf)
                            index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                            container_id = data[index_first_slash:index_second_slash]
            if not container_id:
                return False, "获取容器ID失败！"
            # 重启当前容器
            client.containers.get(container_id.strip()).restart()
            return True, ""
        except Exception as err:
            print(str(err))
            return False, f"重启时发生错误：{str(err)}"

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
