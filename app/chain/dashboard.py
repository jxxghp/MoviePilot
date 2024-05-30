from typing import Optional

from app import schemas
from app.chain import ChainBase
from app.utils.singleton import Singleton


class DashboardChain(ChainBase, metaclass=Singleton):
    """
    各类仪表板统计处理链
    """
    def media_statistic(self) -> Optional[list[schemas.Statistic]]:
        """
        媒体数量统计
        """
        return self.run_module("media_statistic")

    def downloader_info(self) -> Optional[list[schemas.DownloaderInfo]]:
        """
        下载器信息
        """
        return self.run_module("downloader_info")
