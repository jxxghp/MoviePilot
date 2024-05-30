
from app import schemas
from app.chain import ChainBase
from app.core.context import MediaInfo
from app.utils.singleton import Singleton


class BangumiChain(ChainBase, metaclass=Singleton):
    """
    Bangumi处理链，单例运行
    """

    def calendar(self) -> list[MediaInfo] | None:
        """
        获取Bangumi每日放送
        """
        return self.run_module("bangumi_calendar")

    def bangumi_info(self, bangumiid: int) -> dict | None:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return self.run_module("bangumi_info", bangumiid=bangumiid)

    def bangumi_credits(self, bangumiid: int) -> list[schemas.MediaPerson]:
        """
        根据BangumiID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        return self.run_module("bangumi_credits", bangumiid=bangumiid)

    def bangumi_recommend(self, bangumiid: int) -> list[MediaInfo] | None:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self.run_module("bangumi_recommend", bangumiid=bangumiid)

    def person_detail(self, person_id: int) -> schemas.MediaPerson | None:
        """
        根据人物ID查询Bangumi人物详情
        :param person_id:  人物ID
        """
        return self.run_module("bangumi_person_detail", person_id=person_id)

    def person_credits(self, person_id: int) -> list[MediaInfo] | None:
        """
        根据人物ID查询人物参演作品
        :param person_id:  人物ID
        """
        return self.run_module("bangumi_person_credits", person_id=person_id)
