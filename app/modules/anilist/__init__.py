from typing import Any, List, Optional, Tuple, Union

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.runtime.config import settings
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.scraper import MediaScraperHelper
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.modules.anilist.anilist import AniListApi
from app.schemas.types import (
    MediaSource,
    MediaSourceSelection,
    MediaType,
)
from app.domain.media import is_media_source_enabled
from app.schemas.media import normalize_media_source

# 榜单标识到本模块方法名的映射，discover_board 只接受在册标识，白名单校验先于 getattr 完成
_DISCOVER_BOARDS = {
    "trending": "anilist_trending",
    "popular_this_season": "anilist_popular_this_season",
}


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
    def get_priority() -> int:
        """获取模块优先级"""
        return 4

    @staticmethod
    def _source_enabled(media_source: Optional[MediaSource]) -> bool:
        """
        判断本次识别是否指定 AniList。

        :param media_source: 请求级识别数据源
        :return: 是否启用 AniList 识别
        """
        return (media_source or settings.RECOGNIZE_SOURCE) == MediaSource.AniList

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
    def _build_credit_person(cls, edge: dict) -> Optional[_SchemaMediaPerson]:
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
        return _SchemaMediaPerson(
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
    def _build_person_detail(cls, info: dict) -> _SchemaMediaPerson:
        """
        将 AniList 人物详情转换为统一人物信息。

        :param info: AniList 人物详情
        :return: 媒体人物信息
        """
        name_info = info.get("name") or {}
        images = info.get("image") or {}
        return _SchemaMediaPerson(
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
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 统一媒体信息
        """
        # AniList 只处理动画影视，不能在音乐模块未响应时接管音乐请求。
        if (
                kwargs.get("mtype") == MediaType.MUSIC
                or getattr(meta, "type", None) == MediaType.MUSIC
        ):
            return None
        if media_source and media_source != MediaSource.AniList:
            return None
        if media_id is not None and (
                media_source != MediaSource.AniList or not str(media_id).isdigit()
        ):
            return None
        anilistid = int(media_id) if media_id is not None else None
        if not anilistid and (not meta or not self._source_enabled(media_source)):
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
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        异步按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 统一媒体信息
        """
        # 与同步入口保持同一类型边界，音乐请求不得进入 AniList。
        if (
                kwargs.get("mtype") == MediaType.MUSIC
                or getattr(meta, "type", None) == MediaType.MUSIC
        ):
            return None
        if media_source and media_source != MediaSource.AniList:
            return None
        if media_id is not None and (
                media_source != MediaSource.AniList or not str(media_id).isdigit()
        ):
            return None
        anilistid = int(media_id) if media_id is not None else None
        if not anilistid and (not meta or not self._source_enabled(media_source)):
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
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param media_source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if not is_media_source_enabled(media_source, MediaSource.AniList):
            return None
        if not meta or not meta.name:
            return []
        return [
            MediaInfo(anilist_info=self._enrich_people(info))
            for info in self.anilist_api.search(meta.name)
            if self._matches_meta(meta, info)
        ]

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param media_source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if not is_media_source_enabled(media_source, MediaSource.AniList):
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
    ) -> List[_SchemaMediaPerson]:
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
    ) -> List[_SchemaMediaPerson]:
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

    def anilist_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取 AniList 人物详情。

        :param person_id: AniList 人物 ID
        :return: 媒体人物信息
        """
        info = self.anilist_api.person_detail(person_id)
        return self._build_person_detail(info) if info else None

    async def async_anilist_person_detail(
        self, person_id: int
    ) -> Optional[_SchemaMediaPerson]:
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

    def person_detail(self, source: Optional[MediaSource] = None,
                       person_id: int = None,
                       **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情
        :param source: 媒体来源，非AniList来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return self.anilist_person_detail(person_id=person_id)

    async def async_person_detail(self, source: Optional[MediaSource] = None,
                                   person_id: int = None,
                                   **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return await self.async_anilist_person_detail(person_id=person_id)

    def person_credits(self, source: Optional[MediaSource] = None,
                        person_id: int = None,
                        page: int = 1,
                        count: Optional[int] = None,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品
        :param source: 媒体来源，非AniList来源返回 None
        :param person_id: 人物ID
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return self.anilist_person_credits(
            person_id=person_id, page=page, count=count if count is not None else 20
        )

    async def async_person_credits(self, source: Optional[MediaSource] = None,
                                    person_id: int = None,
                                    page: int = 1,
                                    count: Optional[int] = None,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param person_id: 人物ID
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return await self.async_anilist_person_credits(
            person_id=person_id, page=page, count=count if count is not None else 20
        )

    def media_credits(self, source: Optional[MediaSource] = None,
                       media_id: Any = None,
                       mtype: Optional[MediaType] = None,
                       page: int = 1,
                       count: Optional[int] = None,
                       **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.anilist_credits(
            anilist_id, page=page, count=count if count is not None else 20
        )

    async def async_media_credits(self, source: Optional[MediaSource] = None,
                                   media_id: Any = None,
                                   mtype: Optional[MediaType] = None,
                                   page: int = 1,
                                   count: Optional[int] = None,
                                   **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_anilist_credits(
            anilist_id, page=page, count=count if count is not None else 20
        )

    def media_recommend(self, source: Optional[MediaSource] = None,
                         media_id: Any = None,
                         mtype: Optional[MediaType] = None,
                         page: int = 1,
                         count: Optional[int] = None,
                         **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.anilist_recommendations(
            anilist_id, page=page, count=count if count is not None else 20
        )

    async def async_media_recommend(self, source: Optional[MediaSource] = None,
                                     media_id: Any = None,
                                     mtype: Optional[MediaType] = None,
                                     page: int = 1,
                                     count: Optional[int] = None,
                                     **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param page: 页码
        :param count: 每页数量，未指定时使用本源缺省值 20
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_anilist_recommendations(
            anilist_id, page=page, count=count if count is not None else 20
        )

    def discover(self, source: Optional[MediaSource] = None,
                 **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体
        :param source: 媒体来源，非AniList来源返回 None
        :param criteria: 筛选条件，原样转发给 anilist_discover，不补默认值
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return self.anilist_discover(**criteria)

    async def async_discover(self, source: Optional[MediaSource] = None,
                              **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param criteria: 筛选条件，原样转发给 async_anilist_discover，不补默认值
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        return await self.async_anilist_discover(**criteria)

    def discover_board(self, source: Optional[MediaSource] = None,
                        board: str = None,
                        page: int = 1,
                        count: int = 20,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单
        :param source: 媒体来源，非AniList来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 页码
        :param count: 每页数量
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return getattr(self, method_name)(page=page, count=count)

    async def async_discover_board(self, source: Optional[MediaSource] = None,
                                    board: str = None,
                                    page: int = 1,
                                    count: int = 20,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 页码
        :param count: 每页数量
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return await getattr(self, f"async_{method_name}")(page=page, count=count)

    def media_detail(self, source: Optional[MediaSource] = None,
                      media_id: Any = None,
                      mtype: Optional[MediaType] = None,
                      season: Optional[int] = None,
                      raise_exception: bool = False,
                      **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param season: 本源不支持
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.anilist_info(anilist_id=anilist_id)

    async def async_media_detail(self, source: Optional[MediaSource] = None,
                                  media_id: Any = None,
                                  mtype: Optional[MediaType] = None,
                                  season: Optional[int] = None,
                                  raise_exception: bool = False,
                                  **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情（异步版本）
        :param source: 媒体来源，非AniList来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param season: 本源不支持
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.AniList:
            return None
        if media_id is None:
            return None
        try:
            anilist_id = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_anilist_info(anilist_id=anilist_id)
