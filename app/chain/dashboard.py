
from app import schemas
from app.chain import ChainBase
from app.utils.singleton import Singleton


class DashboardChain(ChainBase, metaclass=Singleton):
    """
    各类仪表板统计处理链
    """
    def media_statistic(self) -> list[schemas.Statistic] | None:
        """
        媒体数量统计
        """
        return self.run_module("media_statistic")

    def downloader_info(self) -> list[schemas.DownloaderInfo] | None:
        """
        下载器信息
        """
        return self.run_module("downloader_info")
