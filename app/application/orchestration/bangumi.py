from typing import Optional, List

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource


class BangumiChain(ChainBase):
    """
    Bangumi处理链
    """

    def calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送
        """
        return self.unicast("discover_board", source=MediaSource.Bangumi, board="calendar")

    def discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧
        """
        return self.unicast("discover", source=MediaSource.Bangumi, **kwargs)

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return self.unicast("media_detail", source=MediaSource.Bangumi, media_id=bangumiid)

    def bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据BangumiID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        return self.unicast("media_credits", source=MediaSource.Bangumi, media_id=bangumiid)

    def bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self.unicast("media_recommend", source=MediaSource.Bangumi, media_id=bangumiid)

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询Bangumi人物详情
        :param person_id:  人物ID
        """
        return self.unicast("person_detail", source=MediaSource.Bangumi, person_id=person_id)

    def person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品
        :param person_id:  人物ID
        """
        return self.unicast("person_credits", source=MediaSource.Bangumi, person_id=person_id)

    async def async_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Bangumi, board="calendar")

    async def async_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧（异步版本）
        """
        return await self.async_unicast("async_discover", source=MediaSource.Bangumi, **kwargs)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        return await self.async_unicast(
            "async_media_detail", source=MediaSource.Bangumi, media_id=bangumiid
        )

    async def async_bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据BangumiID查询电影演职员表（异步版本）
        :param bangumiid:  BangumiID
        """
        return await self.async_unicast("async_media_credits", source=MediaSource.Bangumi, media_id=bangumiid)

    async def async_bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影（异步版本）
        :param bangumiid:  BangumiID
        """
        return await self.async_unicast("async_media_recommend", source=MediaSource.Bangumi, media_id=bangumiid)

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询Bangumi人物详情（异步版本）
        :param person_id:  人物ID
        """
        return await self.async_unicast("async_person_detail", source=MediaSource.Bangumi, person_id=person_id)

    async def async_person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        """
        return await self.async_unicast("async_person_credits", source=MediaSource.Bangumi, person_id=person_id)
