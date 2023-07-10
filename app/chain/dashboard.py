from app import schemas
from app.chain import ChainBase


class DashboardChain(ChainBase):
    """
    各类仪表板统计处理链
    """
    def media_statistic(self) -> schemas.Statistic:
        """
        媒体数量统计
        """
        return self.run_module("media_statistic")
