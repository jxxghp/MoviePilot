from typing import List, Optional, Tuple, Union

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.scraper import MediaScraperHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.anilist.anilist import AniListApi
from app.schemas.types import MediaRecognizeType, MediaType, ModuleType


class AniListModule(_ModuleBase):
    """
    AniList 动画媒体识别与刮削模块
    """

    CONFIG_WATCH = {"PROXY_HOST"}

    anilist_api: AniListApi = None
    scraper: MediaScraperHelper = None

    def init_module(self) -> None:
        """初始化 AniList 客户端与通用刮削器"""
        self.anilist_api = AniListApi()
        self.scraper = MediaScraperHelper()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """AniList 模块无需独立开关"""
        return None

    def stop(self) -> None:
        """关闭 AniList 模块"""
        return None

    def test(self) -> Tuple[bool, str]:
        """测试 AniList GraphQL API 连通性"""
        result = self.anilist_api.search("Cowboy Bebop", count=1)
        return (True, "") if result else (False, "AniList网络连接失败")

    @staticmethod
    def get_name() -> str:
        """获取模块名称"""
        return "AniList"

    @staticmethod
    def get_type() -> ModuleType:
        """获取模块类型"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """获取模块子类型"""
        return MediaRecognizeType.AniList

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级"""
        return 4

    @staticmethod
    def _source_enabled(source: Optional[str]) -> bool:
        """
        判断本次识别是否指定 AniList。

        :param source: 请求级识别数据源
        :return: 是否启用 AniList 识别
        """
        return (source or settings.RECOGNIZE_SOURCE) == "anilist"

    @staticmethod
    def _media_type(info: dict) -> MediaType:
        """
        将 AniList 发布格式转换为系统媒体类型。

        :param info: AniList 媒体信息
        :return: 系统媒体类型
        """
        return MediaType.MOVIE if info.get("format") == "MOVIE" else MediaType.TV

    @classmethod
    def _matches_meta(cls, meta: MetaBase, info: dict) -> bool:
        """
        判断 AniList 候选项是否符合标题解析出的类型与年份。

        :param meta: 标题解析元数据
        :param info: AniList 候选项
        :return: 是否符合筛选条件
        """
        if meta.type in {MediaType.MOVIE, MediaType.TV} and cls._media_type(info) != meta.type:
            return False
        year = info.get("startDate", {}).get("year") or info.get("seasonYear")
        return not meta.year or not year or str(year) == str(meta.year)

    @staticmethod
    def _enrich_people(info: dict) -> dict:
        """
        将 AniList 人物连接转换为统一媒体信息所需的演职员结构。

        :param info: AniList 媒体详情
        :return: 补充演员和导演后的媒体详情
        """
        enriched = dict(info)
        actors = []
        for edge in info.get("characters", {}).get("edges") or []:
            character = edge.get("node") or {}
            voice_actors = edge.get("voiceActors") or []
            actor = voice_actors[0] if voice_actors else {}
            actor_name = actor.get("name", {}).get("full")
            if not actor_name:
                continue
            actors.append(
                {
                    "id": actor.get("id"),
                    "name": actor_name,
                    "character": character.get("name", {}).get("full")
                    or character.get("name", {}).get("native"),
                    "avatar": {"large": actor.get("image", {}).get("large")},
                    "url": actor.get("siteUrl"),
                }
            )
        enriched["actors"] = actors

        directors = []
        for edge in info.get("staff", {}).get("edges") or []:
            role = edge.get("role") or ""
            if "Director" not in role:
                continue
            staff = edge.get("node") or {}
            directors.append(
                {
                    "id": staff.get("id"),
                    "name": staff.get("name", {}).get("full"),
                    "job": role,
                    "avatar": {"large": staff.get("image", {}).get("large")},
                    "url": staff.get("siteUrl"),
                }
            )
        enriched["directors"] = directors
        return enriched

    @staticmethod
    def _person_name(name_info: dict) -> Optional[str]:
        """
        按原语言、通用名顺序选择 AniList 人物姓名。

        :param name_info: AniList 人物姓名字段
        :return: 可展示姓名
        """
        return name_info.get("native") or name_info.get("full")

    @staticmethod
    def _person_date(date_info: dict) -> Optional[str]:
        """
        将 AniList 人物模糊日期转换为标准日期文本。

        :param date_info: AniList FuzzyDate 字段
        :return: 日期文本
        """
        return MediaInfo._anilist_date(date_info)

    @classmethod
    def _build_credit_person(cls, edge: dict) -> Optional[schemas.MediaPerson]:
        """
        将 AniList 角色配音关系转换为统一人物信息。

        :param edge: AniList 角色关系边
        :return: 媒体人物信息
        """
        actor = next(iter(edge.get("voiceActors") or []), None)
        if not actor:
            return None
        name_info = actor.get("name") or {}
        character_name = (edge.get("node") or {}).get("name") or {}
        images = actor.get("image") or {}
        return schemas.MediaPerson(
            source="anilist",
            id=actor.get("id"),
            name=cls._person_name(name_info),
            original_name=name_info.get("full"),
            also_known_as=name_info.get("alternative") or [],
            character=character_name.get("native") or character_name.get("full"),
            images=images,
            avatar=images,
            url=actor.get("siteUrl"),
        )

    @classmethod
    def _build_person_detail(cls, info: dict) -> schemas.MediaPerson:
        """
        将 AniList 人物详情转换为统一人物信息。

        :param info: AniList 人物详情
        :return: 媒体人物信息
        """
        name_info = info.get("name") or {}
        images = info.get("image") or {}
        return schemas.MediaPerson(
            source="anilist",
            id=info.get("id"),
            name=cls._person_name(name_info),
            original_name=name_info.get("full"),
            also_known_as=name_info.get("alternative") or [],
            images=images,
            avatar=images,
            biography=info.get("description"),
            birthday=cls._person_date(info.get("dateOfBirth") or {}),
            deathday=cls._person_date(info.get("dateOfDeath") or {}),
            gender=info.get("gender"),
            place_of_birth=info.get("homeTown"),
            career=info.get("primaryOccupations") or [],
            url=info.get("siteUrl"),
        )

    def recognize_media(
        self,
        meta: MetaBase = None,
        anilistid: Optional[int] = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param anilistid: AniList 媒体 ID
        :param source: 请求级识别数据源
        :return: 统一媒体信息
        """
        if not anilistid and (not meta or not self._source_enabled(source)):
            return None
        info = self.anilist_api.detail(anilistid) if anilistid else self._match_by_meta(meta)
        if not info:
            return None
        mediainfo = MediaInfo(anilist_info=self._enrich_people(info))
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        logger.info(
            f"{anilistid or meta.name} AniList识别结果：{mediainfo.type.value} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    async def async_recognize_media(
        self,
        meta: MetaBase = None,
        anilistid: Optional[int] = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        异步按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param anilistid: AniList 媒体 ID
        :param source: 请求级识别数据源
        :return: 统一媒体信息
        """
        if not anilistid and (not meta or not self._source_enabled(source)):
            return None
        info = (
            await self.anilist_api.async_detail(anilistid)
            if anilistid
            else await self._async_match_by_meta(meta)
        )
        if not info:
            return None
        mediainfo = MediaInfo(anilist_info=self._enrich_people(info))
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        logger.info(
            f"{anilistid or meta.name} AniList识别结果：{mediainfo.type.value} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        同步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        for info in self.anilist_api.search(meta.name):
            if self._matches_meta(meta, info):
                return info
        return None

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        异步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        for info in await self.anilist_api.async_search(meta.name):
            if self._matches_meta(meta, info):
                return info
        return None

    def search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if source and source != "anilist":
            return None
        if not source and settings.SEARCH_SOURCE and "anilist" not in settings.SEARCH_SOURCE:
            return None
        if not meta or not meta.name:
            return []
        return [
            MediaInfo(anilist_info=self._enrich_people(info))
            for info in self.anilist_api.search(meta.name)
            if self._matches_meta(meta, info)
        ]

    async def async_search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if source and source != "anilist":
            return None
        if not source and settings.SEARCH_SOURCE and "anilist" not in settings.SEARCH_SOURCE:
            return None
        if not meta or not meta.name:
            return []
        return [
            MediaInfo(anilist_info=self._enrich_people(info))
            for info in await self.anilist_api.async_search(meta.name)
            if self._matches_meta(meta, info)
        ]

    def anilist_info(self, anilist_id: int) -> Optional[dict]:
        """
        获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        return self.anilist_api.detail(anilist_id) if anilist_id else None

    async def async_anilist_info(self, anilist_id: int) -> Optional[dict]:
        """
        异步获取 AniList 动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        return await self.anilist_api.async_detail(anilist_id) if anilist_id else None

    def anilist_trending(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return [
            MediaInfo(anilist_info=info)
            for info in self.anilist_api.trending(page=page, count=count)
        ]

    async def async_anilist_trending(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        异步获取 AniList 当前趋势榜。

        :return: 统一媒体信息列表
        """
        return [
            MediaInfo(anilist_info=info)
            for info in await self.anilist_api.async_trending(page=page, count=count)
        ]

    def anilist_popular_this_season(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        return [
            MediaInfo(anilist_info=info)
            for info in self.anilist_api.popular_this_season(page=page, count=count)
        ]

    async def async_anilist_popular_this_season(
        self, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步获取 AniList 本季热门榜。

        :return: 统一媒体信息列表
        """
        infos = await self.anilist_api.async_popular_this_season(page=page, count=count)
        return [MediaInfo(anilist_info=info) for info in infos]

    def anilist_discover(self, **kwargs) -> List[MediaInfo]:
        """
        按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return [
            MediaInfo(anilist_info=info)
            for info in self.anilist_api.discover(**kwargs)
        ]

    async def async_anilist_discover(self, **kwargs) -> List[MediaInfo]:
        """
        异步按组合条件探索 AniList 动画。

        :return: 统一媒体信息列表
        """
        return [
            MediaInfo(anilist_info=info)
            for info in await self.anilist_api.async_discover(**kwargs)
        ]

    def anilist_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[schemas.MediaPerson]:
        """
        获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        persons = (
            self._build_credit_person(edge)
            for edge in self.anilist_api.credits(anilist_id, page=page, count=count)
        )
        return [person for person in persons if person]

    async def async_anilist_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[schemas.MediaPerson]:
        """
        异步获取 AniList 动画配音演员。

        :return: 媒体人物列表
        """
        edges = await self.anilist_api.async_credits(anilist_id, page=page, count=count)
        persons = (self._build_credit_person(edge) for edge in edges)
        return [person for person in persons if person]

    def anilist_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        infos = self.anilist_api.recommendations(anilist_id, page=page, count=count)
        return [MediaInfo(anilist_info=info) for info in infos]

    async def async_anilist_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步获取 AniList 动画相关推荐。

        :return: 统一媒体信息列表
        """
        infos = await self.anilist_api.async_recommendations(
            anilist_id, page=page, count=count
        )
        return [MediaInfo(anilist_info=info) for info in infos]

    def anilist_person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        获取 AniList 人物详情。

        :param person_id: AniList 人物 ID
        :return: 媒体人物信息
        """
        info = self.anilist_api.person_detail(person_id)
        return self._build_person_detail(info) if info else None

    async def async_anilist_person_detail(
        self, person_id: int
    ) -> Optional[schemas.MediaPerson]:
        """
        异步获取 AniList 人物详情。

        :param person_id: AniList 人物 ID
        :return: 媒体人物信息
        """
        info = await self.anilist_api.async_person_detail(person_id)
        return self._build_person_detail(info) if info else None

    def anilist_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        infos = self.anilist_api.person_credits(person_id, page=page, count=count)
        return [MediaInfo(anilist_info=info) for info in infos]

    async def async_anilist_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步获取 AniList 人物参与的动画作品。

        :return: 统一媒体信息列表
        """
        infos = await self.anilist_api.async_person_credits(
            person_id, page=page, count=count
        )
        return [MediaInfo(anilist_info=info) for info in infos]

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        生成 AniList 来源的 NFO 内容。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: NFO XML 文本
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "anilist":
            return None
        return self.scraper.get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取 AniList 来源的刮削图片清单。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: 图片文件名与下载地址映射
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "anilist":
            return None
        return self.scraper.get_metadata_img(mediainfo, season=season, episode=episode)

    def clear_cache(self) -> None:
        """清理 AniList 接口缓存"""
        self.anilist_api.clear_cache()
