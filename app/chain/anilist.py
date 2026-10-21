"""
AniList 动漫详情、发现、推荐与演职人员能力的模块调度入口。
"""

from typing import Any, Optional, cast

from app.chain.base import ChainBase
from app.domain.context import MediaInfo
from app.schemas.context import MediaPerson as _SchemaMediaPerson


class AniListChain(ChainBase):
    """
    AniList 榜单、探索与深度浏览处理链
    """

    def info(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        result = self.run_module("anilist_info", anilist_id=anilist_id)
        return result if isinstance(result, dict) else None

    async def async_info(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        异步获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        result = await self.async_run_module("async_anilist_info", anilist_id=anilist_id)
        return result if isinstance(result, dict) else None

    def trending(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return self.run_module("anilist_trending", page=page, count=count) or []

    async def async_trending(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        异步获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return await self.async_run_module(
            "async_anilist_trending", page=page, count=count
        ) or []

    def popular_this_season(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        return self.run_module(
            "anilist_popular_this_season", page=page, count=count
        ) or []

    async def async_popular_this_season(
        self, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        return await self.async_run_module(
            "async_anilist_popular_this_season", page=page, count=count
        ) or []

    def discover(self, **kwargs: Any) -> list[MediaInfo]:
        """
        按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return self.run_module("anilist_discover", **kwargs) or []

    async def async_discover(self, **kwargs: Any) -> list[MediaInfo]:
        """
        异步按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return await self.async_run_module("async_anilist_discover", **kwargs) or []

    def credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[_SchemaMediaPerson]:
        """
        获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        return self.run_module(
            "anilist_credits", anilist_id=anilist_id, page=page, count=count
        ) or []

    async def async_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[_SchemaMediaPerson]:
        """
        异步获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        return await self.async_run_module(
            "async_anilist_credits", anilist_id=anilist_id, page=page, count=count
        ) or []

    def recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        return self.run_module(
            "anilist_recommendations", anilist_id=anilist_id, page=page, count=count
        ) or []

    async def async_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        return await self.async_run_module(
            "async_anilist_recommendations",
            anilist_id=anilist_id,
            page=page,
            count=count,
        ) or []

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取 AniList 人物详情。

        :return: 媒体人物信息
        """
        result = self.run_module("anilist_person_detail", person_id=person_id)
        return cast(_SchemaMediaPerson, result) if result else None

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        异步获取 AniList 人物详情。

        :return: 媒体人物信息
        """
        result = await self.async_run_module(
            "async_anilist_person_detail", person_id=person_id
        )
        return cast(_SchemaMediaPerson, result) if result else None

    def person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        return self.run_module(
            "anilist_person_credits", person_id=person_id, page=page, count=count
        ) or []

    async def async_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        return await self.async_run_module(
            "async_anilist_person_credits",
            person_id=person_id,
            page=page,
            count=count,
        ) or []
