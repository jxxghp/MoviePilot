from typing import Optional

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource


class AniListChain(ChainBase):
    """
    AniList 榜单、探索与深度浏览处理链
    """

    def info(self, anilist_id: int) -> Optional[dict]:
        """
        获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        return self.unicast("media_detail", source=MediaSource.AniList, media_id=anilist_id)

    async def async_info(self, anilist_id: int) -> Optional[dict]:
        """
        异步获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        return await self.async_unicast(
            "async_media_detail", source=MediaSource.AniList, media_id=anilist_id
        )

    def trending(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return self.unicast(
            "discover_board", source=MediaSource.AniList, board="trending", page=page, count=count
        ) or []

    async def async_trending(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        异步获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return await self.async_unicast(
            "async_discover_board", source=MediaSource.AniList, board="trending", page=page, count=count
        ) or []

    def popular_this_season(self, page: int = 1, count: int = 20) -> list[MediaInfo]:
        """
        获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        return self.unicast(
            "discover_board", source=MediaSource.AniList, board="popular_this_season", page=page, count=count
        ) or []

    async def async_popular_this_season(
        self, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        return await self.async_unicast(
            "async_discover_board", source=MediaSource.AniList, board="popular_this_season",
            page=page, count=count
        ) or []

    def discover(self, **kwargs) -> list[MediaInfo]:
        """
        按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return self.unicast("discover", source=MediaSource.AniList, **kwargs) or []

    async def async_discover(self, **kwargs) -> list[MediaInfo]:
        """
        异步按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return await self.async_unicast("async_discover", source=MediaSource.AniList, **kwargs) or []

    def credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[_SchemaMediaPerson]:
        """
        获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        return self.unicast(
            "media_credits", source=MediaSource.AniList, media_id=anilist_id, page=page, count=count
        ) or []

    async def async_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[_SchemaMediaPerson]:
        """
        异步获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        return await self.async_unicast(
            "async_media_credits", source=MediaSource.AniList, media_id=anilist_id, page=page, count=count
        ) or []

    def recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        return self.unicast(
            "media_recommend", source=MediaSource.AniList, media_id=anilist_id, page=page, count=count
        ) or []

    async def async_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        return await self.async_unicast(
            "async_media_recommend",
            source=MediaSource.AniList,
            media_id=anilist_id,
            page=page,
            count=count,
        ) or []

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取 AniList 人物详情。

        :return: 媒体人物信息
        """
        return self.unicast("person_detail", source=MediaSource.AniList, person_id=person_id)

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        异步获取 AniList 人物详情。

        :return: 媒体人物信息
        """
        return await self.async_unicast(
            "async_person_detail", source=MediaSource.AniList, person_id=person_id
        )

    def person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        return self.unicast(
            "person_credits", source=MediaSource.AniList, person_id=person_id, page=page, count=count
        ) or []

    async def async_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> list[MediaInfo]:
        """
        异步获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        return await self.async_unicast(
            "async_person_credits",
            source=MediaSource.AniList,
            person_id=person_id,
            page=page,
            count=count,
        ) or []
