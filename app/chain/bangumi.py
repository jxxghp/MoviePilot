"""
Bangumi 媒体搜索、详情与日历能力的模块调度入口。
"""

from typing import Any, List, Optional, cast

from app.chain.base import ChainBase
from app.domain.context import MediaInfo
from app.schemas.context import MediaPerson as _SchemaMediaPerson


class BangumiChain(ChainBase):
    """
    Bangumi处理链
    """

    def calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送
        """
        return cast(Optional[List[MediaInfo]], self.run_module("bangumi_calendar"))

    def discover(self, **kwargs: Any) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧
        """
        return cast(Optional[List[MediaInfo]], self.run_module("bangumi_discover", **kwargs))

    def bangumi_info(self, bangumiid: int) -> Optional[dict[str, Any]]:
        """
        获取Bangumi信息

        :param bangumiid: Bangumi 条目标识
        :return: Bangumi信息
        """
        return cast(Optional[dict[str, Any]], self.run_module("bangumi_info", bangumiid=bangumiid))

    def bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据BangumiID查询电影演职员表

        :param bangumiid: Bangumi 条目标识
        """
        return cast(List[_SchemaMediaPerson], (self.run_module("bangumi_credits", bangumiid=bangumiid)) or [])

    def bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影

        :param bangumiid: Bangumi 条目标识
        """
        return cast(Optional[List[MediaInfo]], self.run_module("bangumi_recommend", bangumiid=bangumiid))

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询Bangumi人物详情

        :param person_id:  人物ID
        """
        return cast(Optional[_SchemaMediaPerson], self.run_module("bangumi_person_detail", person_id=person_id))

    def person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品

        :param person_id:  人物ID
        """
        return cast(Optional[List[MediaInfo]], self.run_module("bangumi_person_credits", person_id=person_id))

    async def async_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送（异步版本）
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_bangumi_calendar"))

    async def async_discover(self, **kwargs: Any) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧（异步版本）
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_bangumi_discover", **kwargs))

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict[str, Any]]:
        """
        获取Bangumi信息（异步版本）

        :param bangumiid: Bangumi 条目标识
        :return: Bangumi信息
        """
        return cast(Optional[dict[str, Any]], await self.async_run_module("async_bangumi_info", bangumiid=bangumiid))

    async def async_bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据BangumiID查询电影演职员表（异步版本）

        :param bangumiid: Bangumi 条目标识
        """
        return cast(List[_SchemaMediaPerson], (await self.async_run_module("async_bangumi_credits", bangumiid=bangumiid)) or [])

    async def async_bangumi_recommend(self, bangumiid: int) -> Optional[List[MediaInfo]]:
        """
        根据BangumiID查询推荐电影（异步版本）

        :param bangumiid: Bangumi 条目标识
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_bangumi_recommend", bangumiid=bangumiid))

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询Bangumi人物详情（异步版本）

        :param person_id:  人物ID
        """
        return cast(Optional[_SchemaMediaPerson], await self.async_run_module("async_bangumi_person_detail", person_id=person_id))

    async def async_person_credits(self, person_id: int) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品（异步版本）

        :param person_id:  人物ID
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_bangumi_person_credits", person_id=person_id))
