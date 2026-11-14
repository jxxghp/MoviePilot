"""
为仪表盘和受限小组件装配同一下载器与本地目录采样口径。
"""

import math
from pathlib import Path

from app.adapters.system.host import SystemUtils
from app.application.directory import DirectoryHelper
from app.chain.dashboard import DashboardChain
from app.schemas.dashboard import DashboardMemoryInfo, DownloaderInfo


def sample_system() -> tuple[float, DashboardMemoryInfo, list[int]]:
    """
    复用仪表盘系统来源，在同一受管工作线程内采样一秒 CPU 与网络区间。

    :return: CPU 百分比、现有进程/系统内存事实及上行/下行字节每秒
    """
    SystemUtils.cpu_usage()
    network = SystemUtils.network_usage()
    return SystemUtils.cpu_usage(), SystemUtils.memory_usage(), network


def _download_free_space(*, btrfs_fsid_dedup: bool, require_source: bool) -> float | None:
    """
    复用目录空间去重，严格读取不把未配置或不完整的容量误报为零。

    :param btrfs_fsid_dedup: 原仪表盘使用的文件系统去重配置
    :param require_source: 小组件需要可确认的完整本地目录来源
    :return: 可确认的剩余字节数；严格模式的未知容量为 None
    :raises OSError: 普通仪表盘保留原有的空间读取异常
    """
    try:
        directories = DirectoryHelper().get_local_download_dirs()
        paths = [Path(directory.download_path) for directory in directories if directory.download_path]
        if require_source and (not paths or not all(path.exists() for path in paths)):
            return None
        total, available = SystemUtils.space_usage(paths, btrfs_fsid_dedup=btrfs_fsid_dedup)
        if require_source and (
            not math.isfinite(total) or not math.isfinite(available) or not 0 <= available <= total or total <= 0
        ):
            return None
        return available
    except OSError:
        if require_source:
            return None
        raise


def _check_speeds(info: DownloaderInfo) -> None:
    """
    拒绝缺失、负数和非有限的原始速度，真实零速率仍是有效采样。

    :param info: 单个下载器或汇总后的原始字节统计
    :raises ValueError: 速率来源不能代表本次真实采样
    """
    if any(
        value is None or not math.isfinite(value) or value < 0 for value in (info.download_speed, info.upload_speed)
    ):
        raise ValueError("下载器速度来源暂不可用")


def sample_downloader(
    name: str | None = None,
    *,
    btrfs_fsid_dedup: bool = False,
    require_source: bool = False,
) -> DownloaderInfo:
    """
    汇总原下载器的字节速率和累计量，包含下载列表之外的做种上传。

    :param name: 原仪表盘可选择的下载器，None 表示全部
    :param btrfs_fsid_dedup: 本次请求的原目录空间去重配置
    :param require_source: 仅小组件严格拒绝缺失或非法速度；普通页面保留零值兼容
    :return: 共用口径的统计，严格模式的未知磁盘容量为 None
    :raises ValueError: 严格模式下缺少真实下载器采样或速度非法
    """
    free_space = _download_free_space(btrfs_fsid_dedup=btrfs_fsid_dedup, require_source=require_source)
    transfer_infos = DashboardChain().downloader_info(name)
    if not transfer_infos:
        if require_source:
            raise ValueError("下载器统计来源暂不可用")
        return DownloaderInfo()
    if require_source:
        for info in transfer_infos:
            _check_speeds(info)
    result = DownloaderInfo(
        download_speed=sum(info.download_speed or 0 for info in transfer_infos),
        upload_speed=sum(info.upload_speed or 0 for info in transfer_infos),
        download_size=sum(info.download_size or 0 for info in transfer_infos),
        upload_size=sum(info.upload_size or 0 for info in transfer_infos),
        free_space=free_space,
    )
    if require_source:
        _check_speeds(result)
    return result
