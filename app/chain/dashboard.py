from typing import Optional, List

from app import schemas
from app.chain import ChainBase
from app.utils.singleton import Singleton


class DashboardChain(ChainBase, metaclass=Singleton):
    """
    各类仪表板统计处理链
    """
    def media_statistic(self, server: Optional[str] = None) -> Optional[List[schemas.Statistic]]:
        """
        媒体数量统计
        """
        return self.run_module("media_statistic", server=server)

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[schemas.DownloaderInfo]]:
        """
        下载器信息
        """
        return self.run_module("downloader_info", downloader=downloader)
