from typing import Optional, List

from app import schemas
from app.chain import ChainBase
from app.core.context import MediaInfo


class BangumiChain(ChainBase):
    """
    Bangumi处理链
    """

    def calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送
        """
        return self.run_module("bangumi_calendar")

    def discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧
        """
        return self.run_module("bangumi_discover", **kwargs)

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return self.run_module("bangumi_info", bangumiid=bangumiid)

    def bangumi_credits(self, bangumiid: int) -> List[schemas.MediaPerson]:
        """
        根据BangumiID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        return self.run_module("bangumi_credits", bangumiid=bangumiid)

    def bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self.run_module("bangumi_recommend", bangumiid=bangumiid)

    def person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        根据人物ID查询Bangumi人物详情
        :param person_id:  人物ID
        """
        return self.run_module("bangumi_person_detail", person_id=person_id)

    def person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品
        :param person_id:  人物ID
        """
        return self.run_module("bangumi_person_credits", person_id=person_id)

    async def async_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送（异步版本）
        """
        return await self.async_run_module("async_bangumi_calendar")

    async def async_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧（异步版本）
        """
        return await self.async_run_module("async_bangumi_discover", **kwargs)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return await self.async_run_module("async_bangumi_info", bangumiid=bangumiid)

    async def async_bangumi_credits(self, bangumiid: int) -> List[schemas.MediaPerson]:
        """
        根据BangumiID查询电影演职员表（异步版本）
        :param bangumiid:  BangumiID
        """
        return await self.async_run_module("async_bangumi_credits", bangumiid=bangumiid)

    async def async_bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影（异步版本）
        :param bangumiid:  BangumiID
        """
        return await self.async_run_module("async_bangumi_recommend", bangumiid=bangumiid)

    async def async_person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        根据人物ID查询Bangumi人物详情（异步版本）
        :param person_id:  人物ID
        """
        return await self.async_run_module("async_bangumi_person_detail", person_id=person_id)

    async def async_person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        """
        return await self.async_run_module("async_bangumi_person_credits", person_id=person_id)
