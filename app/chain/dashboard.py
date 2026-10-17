"""
仪表盘所需媒体库统计和下载器运行状态的汇总。
"""

from typing import List, Optional, cast

from app.chain.base import ChainBase
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.dashboard import Statistic as _SchemaStatistic


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
