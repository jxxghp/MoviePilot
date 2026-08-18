from typing import Optional, List

from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.application.orchestration import ChainBase


class DashboardChain(ChainBase):
    """
    各类仪表板统计处理链
    """
    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        媒体数量统计，合并各媒体服务器提供的统计列表
        """
        return [
            item
            for result in self.multicast("media_statistic", server=server)
            for item in result
        ]

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        下载器信息，合并各下载器提供的信息列表
        """
        return [
            item
            for result in self.multicast("downloader_info", downloader=downloader)
            for item in result
        ]
