from typing import Optional, List

from app.chain import ChainBase
from app.utils.singleton import Singleton


class BangumiChain(ChainBase, metaclass=Singleton):
    """
    Bangumi处理链，单例运行
    """

    def calendar(self, page: int = 1, count: int = 30) -> Optional[List[dict]]:
        """
        获取Bangumi每日放送
        :param page:  页码
        :param count:  每页数量
        """
        return self.run_module("bangumi_calendar", page=page, count=count)

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return self.run_module("bangumi_info", bangumiid=bangumiid)

    def bangumi_credits(self, bangumiid: int, page: int = 1, count: int = 20) -> List[dict]:
        """
        根据BangumiID查询电影演职员表
        :param bangumiid:  BangumiID
        :param page:  页码
        :param count:  数量
        """
        return self.run_module("bangumi_credits", bangumiid=bangumiid, page=page, count=count)

    def bangumi_recommend(self, bangumiid: int) -> List[dict]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self.run_module("bangumi_recommend", bangumiid=bangumiid)
