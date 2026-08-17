"""宿主运行形态探针：判断容器/冻结二进制运行环境、CPU 架构与配置文件路径，仅依赖标准库。"""

import os
import platform
import sys
from pathlib import Path
from typing import Optional


def is_docker() -> bool:
    """
    判断当前进程是否运行在 Docker 容器内。

    返回:
        bool: 存在 /.dockerenv 文件时为 True。
    """
    return Path("/.dockerenv").exists()


def is_frozen() -> bool:
    """
    判断当前进程是否为 PyInstaller 等工具打包出的冻结二进制。

    返回:
        bool: sys.frozen 为真时为 True。
    """
    return getattr(sys, "frozen", False)


def _is_x86_64() -> bool:
    """
    判断 CPU 架构是否为 AMD64/x86_64。

    返回:
        bool: platform.machine() 归一化后属于 amd64/x86_64 时为 True。
    """
    return platform.machine().lower() in ("amd64", "x86_64")


def _is_x86_32() -> bool:
    """
    判断 CPU 架构是否为 x86_32。

    返回:
        bool: platform.machine() 归一化后属于 i386/i686/x86/386/x86_32 时为 True。
    """
    return platform.machine().lower() in ("i386", "i686", "x86", "386", "x86_32")


def _is_aarch64() -> bool:
    """
    判断 CPU 架构是否为 ARM64。

    返回:
        bool: platform.machine() 归一化后属于 aarch64/arm64 时为 True。
    """
    return platform.machine().lower() in ("aarch64", "arm64")


def _is_aarch() -> bool:
    """
    判断 CPU 架构是否为 ARM32。

    返回:
        bool: platform.machine() 以 arm/aarch 开头且不属于 ARM64 时为 True。
    """
    arch_name = platform.machine().lower()
    return arch_name.startswith(("arm", "aarch")) and arch_name not in ("aarch64", "arm64")


def cpu_arch() -> str:
    """
    获取 CPU 架构标识。

    返回:
        str: "x86_64"、"x86_32"、"Arm64"、"Arm32" 之一；均不匹配时返回 platform.machine() 原始值。
    """
    if _is_x86_64():
        return "x86_64"
    elif _is_x86_32():
        return "x86_32"
    elif _is_aarch64():
        return "Arm64"
    elif _is_aarch():
        return "Arm32"
    else:
        return platform.machine()


def _get_config_path(config_dir: Optional[str] = None) -> Path:
    """
    解析配置目录路径。

    参数:
        config_dir (Optional[str]): 显式指定的配置目录，为空时读取 CONFIG_DIR 环境变量。

    返回:
        Path: 按显式参数、CONFIG_DIR 环境变量、Docker 容器、冻结二进制运行环境依次判定得到的配置目录；
            均不满足时回退到源码仓库根目录下的 config 目录。
    """
    if not config_dir:
        config_dir = os.getenv("CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    if is_docker():
        return Path("/config")
    elif is_frozen():
        return Path(sys.executable).parent / "config"
    else:
        return Path(__file__).resolve().parents[2] / "config"


def get_env_path() -> Path:
    """
    获取环境变量配置文件路径。

    返回:
        Path: 配置目录下的 app.env 文件路径。
    """
    return _get_config_path() / "app.env"
