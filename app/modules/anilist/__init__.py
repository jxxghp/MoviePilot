"""
AniList 动画识别、榜单浏览、人物关系与媒体刮削模块。
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union

from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.domain.projection.anilist import format_date as format_anilist_date
from app.domain.scraper import MediaScraperHelper
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.anilist.anilist import AniListApi
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.context import MediaImageSet
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.types import (
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)


@dataclass(frozen=True, slots=True)
class _AniListRecognitionPlan:
    """
    一次 AniList 识别使用的明确身份或标题线索。

    :param media_id: 已校验的正整数标识，None 表示标题搜索
    :param meta: 标题、类型、年份和季号线索，仅按身份查询时可为 None
    :param label: 用于日志展示的标识或搜索名称
    """

    media_id: Optional[int]
    meta: Optional[MetaBase]
    label: str

    def require_meta(self) -> MetaBase:
        """
        读取标题搜索必须具备的媒体线索。

        :return: 计划保存的媒体线索
        :raises RuntimeError: 计划不包含 meta，调用方错误进入标题搜索分支
        """
        if self.meta is None:
            raise RuntimeError("AniList 标题识别计划缺少解析元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _AniListSearchPlan:
    """
    来源选择与有效搜索线索的检查结果。

    :param enabled: 本次来源选择是否启用 AniList
    :param meta: 有效的标题线索；未启用或标题缺失时为 None
    """

    enabled: bool
    meta: Optional[MetaBase]


class AniListModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """
    AniList 动画媒体识别与刮削模块。
    """

    auxiliary_media_source = MediaSource.AniList
    CONFIG_WATCH = {"PROXY_HOST"}

    anilist_api: Optional[AniListApi] = None
    scraper: Optional[MediaScraperHelper] = None

    def _require_api(self) -> AniListApi:
        """
        取得已激活的 AniList 客户端，避免把尚未初始化的兼容属性当作有效实例。

        :return: 模块初始化时创建的客户端
        :raises RuntimeError: 模块尚未初始化
        """
        if self.anilist_api is None:
            raise RuntimeError("AniList 模块尚未初始化")
        return self.anilist_api

    def _require_scraper(self) -> MediaScraperHelper:
        """
        取得模块初始化时创建的刮削器。

        :return: 已初始化的刮削器
        :raises RuntimeError: 模块尚未初始化
        """
        if self.scraper is None:
            raise RuntimeError("AniList 刮削器尚未初始化")
        return self.scraper

    def init_module(self) -> None:
        """
        初始化 AniList 客户端与通用刮削器。
        """
        self.anilist_api = AniListApi()
        self.scraper = MediaScraperHelper()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        AniList 模块无需独立开关。
        """
        return None

    def stop(self) -> None:
        """
        结束模块生命周期，当前实现没有额外关闭动作。
        """
        return None

    def test(self) -> Tuple[bool, str]:
        """
        测试 AniList GraphQL API 连通性。
        """
        result = self._require_api().search("Cowboy Bebop", count=1)
        return (True, "") if result else (False, "AniList网络连接失败")

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "AniList"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型。
        """
        return MediaRecognizeType.AniList

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级。
        """
        return 4

    @staticmethod
    def _source_enabled(media_source: Optional[MediaSource]) -> bool:
        """
        判断本次识别是否指定 AniList。

        :param media_source: 请求级识别数据源
        :return: 是否启用 AniList 识别
        """
        return (media_source or get_runtime_setting('RECOGNIZE_SOURCE')) == MediaSource.AniList

    @classmethod
    def _recognition_plan(
        cls,
        meta: Optional[MetaBase],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        requested_type: Optional[MediaType],
    ) -> Optional[_AniListRecognitionPlan]:
        """
        统一校验来源、媒体类型和显式 AniList ID。
        """
        if requested_type == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.AniList:
            return None
        if media_id is not None:
            if media_source != MediaSource.AniList or not str(media_id).isdigit():
                return None
            normalized_id = int(media_id)
            if normalized_id <= 0:
                return None
            return _AniListRecognitionPlan(
                media_id=normalized_id, meta=meta, label=str(normalized_id)
            )
        if not meta or not cls._source_enabled(media_source):
            return None
        return _AniListRecognitionPlan(media_id=None, meta=meta, label=meta.name)

    @staticmethod
    def _search_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSourceSelection],
    ) -> _AniListSearchPlan:
        """
        统一决定 AniList 搜索是否响应以及是否具备查询标题。
        """
        enabled = is_media_source_enabled(media_source, MediaSource.AniList)
        return _AniListSearchPlan(
            enabled=enabled,
            meta=meta if enabled and meta and meta.name else None,
        )

    @staticmethod
    def _media_type(info: dict[str, Any]) -> MediaType:
        """
        将 AniList 发布格式转换为系统媒体类型。

        :param info: AniList 媒体信息
        :return: 系统媒体类型
        """
        return MediaType.MOVIE if info.get("format") == "MOVIE" else MediaType.TV

    @classmethod
    def _matches_meta(cls, meta: MetaBase, info: dict[str, Any]) -> bool:
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

    @classmethod
    def _select_matching_info(
        cls, meta: MetaBase, infos: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """
        从已获取的候选列表中选择首个类型与年份均匹配的条目。
        """
        return next((info for info in infos if cls._matches_meta(meta, info)), None)

    @classmethod
    def _build_recognized_media(
        cls, info: dict[str, Any], meta: Optional[MetaBase]
    ) -> MediaInfo:
        """
        把识别详情投影为统一媒体信息并保留解析季号。
        """
        mediainfo = MediaInfo(anilist_info=cls._enrich_people(info))
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        return mediainfo

    @classmethod
    def _project_search_results(
        cls, meta: MetaBase, infos: list[dict[str, Any]]
    ) -> list[MediaInfo]:
        """
        过滤 AniList 搜索候选并投影为统一媒体信息列表。
        """
        return [
            MediaInfo(anilist_info=cls._enrich_people(info))
            for info in infos
            if cls._matches_meta(meta, info)
        ]

    @staticmethod
    def _project_media_list(infos: list[dict[str, Any]]) -> list[MediaInfo]:
        """
        把 AniList 媒体列表投影为统一媒体信息列表。
        """
        return [MediaInfo(anilist_info=info) for info in infos]

    @staticmethod
    def _enrich_people(info: dict[str, Any]) -> dict[str, Any]:
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
    def _person_name(name_info: dict[str, Any]) -> Optional[str]:
        """
        按原语言、通用名顺序选择 AniList 人物姓名。

        :param name_info: AniList 人物姓名字段
        :return: 可展示姓名
        """
        return name_info.get("native") or name_info.get("full")

    @staticmethod
    def _person_date(date_info: dict[str, Any]) -> Optional[str]:
        """
        将 AniList 人物模糊日期转换为标准日期文本。

        :param date_info: AniList FuzzyDate 字段
        :return: 日期文本
        """
        return format_anilist_date(date_info)

    @classmethod
    def _build_credit_person(cls, edge: dict[str, Any]) -> Optional[_SchemaMediaPerson]:
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
        image_set = MediaImageSet(**images) if isinstance(images, dict) else None
        return _SchemaMediaPerson(
            source="anilist",
            id=actor.get("id"),
            name=cls._person_name(name_info),
            original_name=name_info.get("full"),
            also_known_as=name_info.get("alternative") or [],
            character=character_name.get("native") or character_name.get("full"),
            images=image_set,
            avatar=image_set,
            url=actor.get("siteUrl"),
        )

    @classmethod
    def _build_person_detail(cls, info: dict[str, Any]) -> _SchemaMediaPerson:
        """
        将 AniList 人物详情转换为统一人物信息。

        :param info: AniList 人物详情
        :return: 媒体人物信息
        """
        name_info = info.get("name") or {}
        images = info.get("image") or {}
        image_set = MediaImageSet(**images) if isinstance(images, dict) else None
        return _SchemaMediaPerson(
            source="anilist",
            id=info.get("id"),
            name=cls._person_name(name_info),
            original_name=name_info.get("full"),
            also_known_as=name_info.get("alternative") or [],
            images=image_set,
            avatar=image_set,
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
        meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[MediaInfo]:
        """
        按 AniList 标识或标题线索识别动画。

        :param meta: 标题、类型、年份和可选季号线索
        :param media_source: 明确来源；省略时标题识别使用全局来源配置
        :param media_id: AniList 正整数标识，仅在明确选择 AniList 时接受
        :param kwargs: mtype 指定请求媒体类型，音乐请求不处理；其他参数不使用
        :return: 统一媒体模型；请求不适用、无匹配或查询失败时为 None
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            self._require_api().detail(plan.media_id)
            if plan.media_id is not None
            else self._match_by_meta(plan.require_meta())
        )
        if not info:
            return None
        mediainfo = self._build_recognized_media(info, plan.meta)
        logger.info(
            f"{plan.label} AniList识别结果：{mediainfo.type.value if mediainfo.type else ''} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    async def async_recognize_media(
        self,
        meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[MediaInfo]:
        """
        异步按 AniList 标识或标题线索识别动画。

        :param meta: 标题、类型、年份和可选季号线索
        :param media_source: 明确来源；省略时标题识别使用全局来源配置
        :param media_id: AniList 正整数标识，仅在明确选择 AniList 时接受
        :param kwargs: mtype 指定请求媒体类型，音乐请求不处理；其他参数不使用
        :return: 统一媒体模型；请求不适用、无匹配或查询失败时为 None
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            await self._require_api().async_detail(plan.media_id)
            if plan.media_id is not None
            else await self._async_match_by_meta(plan.require_meta())
        )
        if not info:
            return None
        mediainfo = self._build_recognized_media(info, plan.meta)
        logger.info(
            f"{plan.label} AniList识别结果：{mediainfo.type.value if mediainfo.type else ''} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict[str, Any]]:
        """
        同步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        return self._select_matching_info(meta, self._require_api().search(meta.name))

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict[str, Any]]:
        """
        异步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        infos = await self._require_api().async_search(meta.name)
        return self._select_matching_info(meta, infos)

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        查询并筛选 AniList 动画候选。

        :param meta: 包含标题的媒体线索
        :param media_source: 本次搜索启用的来源选择条件
        :return: 统一媒体列表；来源未启用时为 None，标题缺失或无匹配时为空列表
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        return self._project_search_results(
            plan.meta, self._require_api().search(plan.meta.name)
        )

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步查询并筛选 AniList 动画候选。

        :param meta: 包含标题的媒体线索
        :param media_source: 本次搜索启用的来源选择条件
        :return: 统一媒体列表；来源未启用时为 None，标题缺失或无匹配时为空列表
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        infos = await self._require_api().async_search(plan.meta.name)
        return self._project_search_results(plan.meta, infos)

    def anilist_info(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        读取原始 AniList 动画详情。

        :param anilist_id: AniList 动画条目标识
        :return: 原始详情映射；标识为空或请求失败时为 None
        """
        return self._require_api().detail(anilist_id) if anilist_id else None

    async def async_anilist_info(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        异步读取原始 AniList 动画详情。

        :param anilist_id: AniList 动画条目标识
        :return: 原始详情映射；标识为空或请求失败时为 None
        """
        return await self._require_api().async_detail(anilist_id) if anilist_id else None

    def anilist_trending(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        查询当前动画趋势榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；请求失败时为空列表
        """
        return self._project_media_list(
            self._require_api().trending(page=page, count=count)
        )

    async def async_anilist_trending(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        异步查询当前动画趋势榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；请求失败时为空列表
        """
        infos = await self._require_api().async_trending(page=page, count=count)
        return self._project_media_list(infos)

    def anilist_popular_this_season(self, page: int = 1, count: int = 20) -> List[MediaInfo]:
        """
        查询本地当前季度的动画人气榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；请求失败时为空列表
        """
        return self._project_media_list(
            self._require_api().popular_this_season(page=page, count=count)
        )

    async def async_anilist_popular_this_season(
        self, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步查询本地当前季度的动画人气榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；请求失败时为空列表
        """
        infos = await self._require_api().async_popular_this_season(page=page, count=count)
        return self._project_media_list(infos)

    def anilist_discover(self, **kwargs: Any) -> List[MediaInfo]:
        """
        按组合条件探索动画。

        :param kwargs: 传入客户端的分页与筛选条件，如 page、count、genre、season_year
        :return: 统一媒体列表；无结果或请求失败时为空列表
        """
        return self._project_media_list(self._require_api().discover(**kwargs))

    async def async_anilist_discover(self, **kwargs: Any) -> List[MediaInfo]:
        """
        异步按组合条件探索动画。

        :param kwargs: 传入客户端的分页与筛选条件，如 page、count、genre、season_year
        :return: 统一媒体列表；无结果或请求失败时为空列表
        """
        infos = await self._require_api().async_discover(**kwargs)
        return self._project_media_list(infos)

    def anilist_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[_SchemaMediaPerson]:
        """
        查询动画的日语配音演员。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一人物列表，跳过无法转换的角色关系；无结果时为空列表
        """
        persons = (
            self._build_credit_person(edge)
            for edge in self._require_api().credits(anilist_id, page=page, count=count)
        )
        return [person for person in persons if person]

    async def async_anilist_credits(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[_SchemaMediaPerson]:
        """
        异步查询动画的日语配音演员。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一人物列表，跳过无法转换的角色关系；无结果时为空列表
        """
        edges = await self._require_api().async_credits(anilist_id, page=page, count=count)
        persons = (self._build_credit_person(edge) for edge in edges)
        return [person for person in persons if person]

    def anilist_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        读取动画相关推荐。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 按推荐顺序排列的统一媒体列表；无结果时为空列表
        """
        infos = self._require_api().recommendations(anilist_id, page=page, count=count)
        return self._project_media_list(infos)

    async def async_anilist_recommendations(
        self, anilist_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步读取动画相关推荐。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 按推荐顺序排列的统一媒体列表；无结果时为空列表
        """
        infos = await self._require_api().async_recommendations(
            anilist_id, page=page, count=count
        )
        return self._project_media_list(infos)

    def anilist_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        查询 AniList 人物详情。

        :param person_id: AniList 人物标识
        :return: 统一人物模型；无结果或查询失败时为 None
        """
        info = self._require_api().person_detail(person_id)
        return self._build_person_detail(info) if info else None

    async def async_anilist_person_detail(
        self, person_id: int
    ) -> Optional[_SchemaMediaPerson]:
        """
        异步查询 AniList 人物详情。

        :param person_id: AniList 人物标识
        :return: 统一人物模型；无结果或查询失败时为 None
        """
        info = await self._require_api().async_person_detail(person_id)
        return self._build_person_detail(info) if info else None

    def anilist_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        查询 AniList 人物的配音作品。

        :param person_id: AniList 人物标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；无结果或查询失败时为空列表
        """
        infos = self._require_api().person_credits(person_id, page=page, count=count)
        return self._project_media_list(infos)

    async def async_anilist_person_credits(
        self, person_id: int, page: int = 1, count: int = 20
    ) -> List[MediaInfo]:
        """
        异步查询 AniList 人物的配音作品。

        :param person_id: AniList 人物标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 统一媒体列表；无结果或查询失败时为空列表
        """
        infos = await self._require_api().async_person_credits(
            person_id, page=page, count=count
        )
        return self._project_media_list(infos)

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """
        为选择 AniList 刮削来源的媒体生成 NFO。

        :param mediainfo: 统一媒体信息，scrape_source 优先于全局配置
        :param season: 可选季号，省略时生成媒体级内容
        :param episode: 可选集号，与季号共同决定内容范围
        :param kwargs: 统一刮削接口的额外参数，当前不读取
        :return: 包含 UTF-8 编码声明的 XML 文本；刮削来源不符或无法生成时为 None
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "anilist":
            return None
        return self._require_scraper().get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        为选择 AniList 刮削来源的媒体生成图片下载清单。

        :param mediainfo: 统一媒体信息，scrape_source 优先于全局配置
        :param season: 可选季号
        :param episode: 可选集号
        :return: 图片文件名到地址的映射；刮削来源不符时为 None
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "anilist":
            return None
        return self._require_scraper().get_metadata_img(mediainfo, season=season, episode=episode)

    def clear_cache(self) -> None:
        """
        清理 AniList 接口缓存。
        """
        self._require_api().clear_cache()
