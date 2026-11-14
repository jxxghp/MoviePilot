"""
仪表盘所需媒体库统计和下载器运行状态的汇总。
"""

from typing import List, Optional, cast

from app.application.directory import DirectoryHelper
from app.chain.base import ChainBase
from app.chain.storage import StorageChain
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.dashboard import Storage as _SchemaStorage
from app.schemas.types import StorageAction


class DashboardChain(ChainBase):
    """
    各类仪表板统计处理链
    """

    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        媒体数量统计
        """
        return cast(
            Optional[List[_SchemaStatistic]],
            self.run_module("media_statistic", server=server),
        )

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        下载器信息
        """
        return cast(
            Optional[List[_SchemaDownloaderInfo]],
            self.run_module("downloader_info", downloader=downloader),
        )

    def storage(self, *, require_source: bool = False) -> _SchemaStorage:
        """
        复用仪表板按存储去重的容量汇总，小组件可要求来源完整可用。

        :param require_source: 来源缺失或任一读取失败时抛出错误；网页保留历史宽容语义
        :return: 存储总量与已用容量，单位为字节
        :raises ValueError: 严格读取缺少来源或来源未成功报告容量
        """
        directories = DirectoryHelper().get_dirs()
        storages = set(
            [item.storage for item in directories if item.download_path and item.storage]
            + [item.library_storage for item in directories if item.library_path and item.library_storage]
        )
        if require_source and not storages:
            raise ValueError("尚未配置存储统计来源")
        total, available = 0, 0
        for storage in storages:
            result = StorageChain().manage_storage(storage=storage, action=StorageAction.USAGE.value)
            usage = result.get("data") if result.get("success") else None
            if require_source and (not usage or usage.get("total") is None or usage.get("available") is None):
                raise ValueError("存储统计来源暂不可用")
            if usage:
                total += usage.get("total") or 0
                available += usage.get("available") or 0
        return _SchemaStorage(total_storage=total, used_storage=total - available)
