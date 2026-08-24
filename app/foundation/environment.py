"""不依赖运行时和适配器的宿主环境探测原语。"""

import os
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Optional


def is_docker() -> bool:
    """判断当前进程是否运行在约定的 Docker 环境中。"""
    return Path("/.dockerenv").exists()


def is_frozen() -> bool:
    """判断当前 Python 进程是否为冻结二进制。"""
    return bool(getattr(sys, "frozen", False))


def is_free_threaded_runtime() -> bool:
    """判断当前解释器是否为 CPython free-threaded 构建。"""
    return sysconfig.get_config_var("Py_GIL_DISABLED") == 1


def is_windows() -> bool:
    """判断当前操作系统是否为 Windows。"""
    return os.name == "nt"


def is_macos() -> bool:
    """判断当前操作系统是否为 macOS。"""
    return platform.system() == "Darwin"


def is_aarch64() -> bool:
    """判断当前 CPU 是否属于 64 位 ARM 架构。"""
    return platform.machine().lower() in {"aarch64", "arm64"}


def is_aarch() -> bool:
    """判断当前 CPU 是否属于非 64 位 ARM 架构。"""
    arch_name = platform.machine().lower()
    return arch_name.startswith(("arm", "aarch")) and not is_aarch64()


def is_x86_64() -> bool:
    """判断当前 CPU 是否属于 64 位 x86 架构。"""
    return platform.machine().lower() in {"amd64", "x86_64"}


def is_x86_32() -> bool:
    """判断当前 CPU 是否属于 32 位 x86 架构。"""
    return platform.machine().lower() in {"i386", "i686", "x86", "386", "x86_32"}


def cpu_arch() -> str:
    """返回 MoviePilot 既有合同使用的 CPU 架构名称。"""
    if is_x86_64():
        return "x86_64"
    if is_x86_32():
        return "x86_32"
    if is_aarch64():
        return "Arm64"
    if is_aarch():
        return "Arm32"
    return platform.machine()


def get_config_path(config_dir: Optional[str] = None) -> Path:
    """按显式目录、容器、冻结进程和源码运行顺序确定配置目录。"""
    configured = config_dir or os.getenv("CONFIG_DIR")
    if configured:
        return Path(configured)
    if is_docker():
        return Path("/config")
    if is_frozen():
        return Path(sys.executable).parent / "config"
    return Path(__file__).resolve().parents[2] / "config"


def get_env_path(config_dir: Optional[str] = None) -> Path:
    """返回给定运行环境对应的 ``app.env`` 文件路径。"""
    return get_config_path(config_dir) / "app.env"
