import os
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.utils.system import SystemUtils


def count_directory_entries(directory: Path, max_check: int = 10000) -> Tuple[int, int]:
    """
    统计目录下的文件与子目录数量（用于检测是否超过系统限制）。
    :param directory: 目录路径
    :param max_check: 最大检查文件数量，避免长时间阻塞
    :return: (文件数量, 目录数量)
    """
    file_count = 0
    dir_count = 0
    try:
        for _, dirs, files in os.walk(str(directory)):
            file_count += len(files)
            dir_count += len(dirs)
            if file_count > max_check:
                break
    except Exception as err:
        logger.debug(f"统计目录规模失败: {err}")
    return file_count, dir_count


def count_directory_files(directory: Path, max_check: int = 10000) -> int:
    """
    统计目录下的文件数量。
    :param directory: 目录路径
    :param max_check: 最大检查数量，避免长时间阻塞
    :return: 文件数量
    """
    file_count, _ = count_directory_entries(directory, max_check=max_check)
    return file_count


def check_system_limits() -> Dict[str, Any]:
    """
    检查系统监控相关限制。
    :return: 系统限制信息
    """
    limits = {
        'max_user_watches': 0,
        'max_user_instances': 0,
        'warnings': []
    }

    try:
        if platform.system() == 'Linux':
            # 检查 inotify 限制
            try:
                with open('/proc/sys/fs/inotify/max_user_watches', 'r', encoding='utf-8', errors='replace') as f:
                    limits['max_user_watches'] = int(f.read().strip())
            except Exception as e:
                logger.debug(f"读取 inotify 限制失败: {e}")
                limits['max_user_watches'] = 8192  # 默认值

            try:
                with open('/proc/sys/fs/inotify/max_user_instances', 'r', encoding='utf-8', errors='replace') as f:
                    limits['max_user_instances'] = int(f.read().strip())
            except Exception as e:
                logger.debug(f"读取 inotify 实例限制失败: {e}")
    except Exception as e:
        limits['warnings'].append(f"检查系统限制时出错: {e}")

    return limits


def get_system_optimization_tips() -> List[str]:
    """
    获取系统优化建议。
    :return: 优化建议列表
    """
    tips = []
    system = platform.system()

    if system == 'Linux':
        tips.extend([
            "增加 inotify 监控数量限制:",
            "echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf",
            "echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf",
            "sudo sysctl -p",
            "",
            "如果在Docker中运行，请在宿主机上执行以上命令"
        ])
    elif system == 'Darwin':
        tips.extend([
            "macOS 系统优化建议:",
            "sudo sysctl kern.maxfiles=65536",
            "sudo sysctl kern.maxfilesperproc=32768",
            "ulimit -n 32768"
        ])
    elif system == 'Windows':
        tips.extend([
            "Windows 系统优化建议:",
            "1. 关闭不必要的实时保护软件对监控目录的扫描",
            "2. 将监控目录添加到Windows Defender排除列表",
            "3. 确保有足够的可用内存"
        ])

    return tips


def decide_monitor_mode(directory: Path,
                        monitor_mode: str) -> Tuple[bool, str, Optional[Dict[str, Any]], Optional[int]]:
    """
    决策监控模式。兼容模式与网络文件系统直接短路，只有快速模式候选才统计
    目录规模与系统限制，避免启动期对网络挂载做无谓的全量遍历。

    inotify 的 max_user_watches 按监视点（目录）计数，因此用目录数量而不是
    文件数量与上限比较。

    :param directory: 监控目录
    :param monitor_mode: 配置的监控模式
    :return: (是否使用轮询, 原因, 系统限制信息或None, 文件数量或None)
    """
    if monitor_mode == "compatibility":
        return True, "用户配置为兼容模式", None, None

    # 检查网络文件系统
    if SystemUtils.is_network_filesystem(directory):
        return True, "检测到网络文件系统，建议使用兼容模式", None, None

    limits = check_system_limits()
    file_count, dir_count = count_directory_entries(directory)
    max_watches = limits.get('max_user_watches')
    if max_watches and dir_count > max_watches * 0.8:
        return (True, f"目录数量({dir_count})接近 inotify 监控上限({max_watches})",
                limits, file_count)
    return False, "使用快速模式", limits, file_count
