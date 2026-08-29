from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union

from app.adapters.network.http import RequestUtils
from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.domain.projection.bangumi import resolve_media_type as resolve_bangumi_media_type
from app.domain.scraper import MediaScraperHelper
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.bangumi.bangumi import BangumiApi
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.types import (
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)


@dataclass(frozen=True, slots=True)
class BangumiConfigSnapshot:
    """Bangumi 模块一次配置 generation 使用的稳定网络快照。"""

    proxy: Any


@dataclass(frozen=True, slots=True)
class _BangumiRecognitionPlan:
    """描述一次 Bangumi 识别应走显式详情还是标题搜索。"""

    media_id: Optional[int]
    meta: Optional[MetaBase]
    label: str

    def require_meta(self) -> MetaBase:
        """返回标题搜索计划必有的解析元数据。"""
        if self.meta is None:
            raise RuntimeError("Bangumi 标题识别计划缺少解析元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _BangumiSearchPlan:
    """保存 Bangumi 搜索来源准入结果和有效查询元数据。"""

    enabled: bool
    meta: Optional[MetaBase]


class BangumiModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """
    Bangumi媒体信息匹配
    """
    auxiliary_media_source = MediaSource.Bangumi
    CONFIG_WATCH = {"PROXY_HOST"}

    bangumiapi: BangumiApi = None
    scraper: MediaScraperHelper = None
    _config: BangumiConfigSnapshot = BangumiConfigSnapshot(proxy=None)

    def init_module(self) -> None:
        """
        初始化Bangumi客户端
        """
        self._config = BangumiConfigSnapshot(proxy=get_runtime_setting('PROXY'))
        self.bangumiapi = BangumiApi()
        self.scraper = MediaScraperHelper()

    def stop(self) -> None:
        """
        关闭Bangumi客户端
        """
        if self.bangumiapi:
            self.bangumiapi.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils(proxies=self._config.proxy).get_res("https://api.bgm.tv/")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接Bangumi，错误码：{ret.status_code}"
        return False, "Bangumi网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """Bangumi模块无需独立开关"""
        return None

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        return "Bangumi"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型
        """
        return MediaRecognizeType.Bangumi

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    @staticmethod
    def _recognition_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        requested_type: Optional[MediaType],
    ) -> Optional[_BangumiRecognitionPlan]:
        """统一校验来源、媒体类型和显式 Bangumi ID。"""
        if requested_type == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.Bangumi:
            return None
        if media_id is not None:
            if media_source != MediaSource.Bangumi or not str(media_id).isdigit():
                return None
            normalized_id = int(media_id)
            if normalized_id <= 0:
                return None
            return _BangumiRecognitionPlan(
                media_id=normalized_id, meta=meta, label=str(normalized_id)
            )
        selected_source = media_source or get_runtime_setting('RECOGNIZE_SOURCE')
        if not meta or selected_source != MediaSource.Bangumi:
            return None
        return _BangumiRecognitionPlan(media_id=None, meta=meta, label=meta.name)

    @staticmethod
    def _search_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSourceSelection],
    ) -> _BangumiSearchPlan:
        """统一决定 Bangumi 搜索是否响应以及是否具备查询标题。"""
        enabled = is_media_source_enabled(media_source, MediaSource.Bangumi)
        return _BangumiSearchPlan(
            enabled=enabled,
            meta=meta if enabled and meta and meta.name else None,
        )

    @staticmethod
    def _candidate_ids(
        infos: Optional[list[dict[str, Any]]],
    ) -> tuple[int, ...]:
        """提取前十个可用于详情查询的 Bangumi 数字 ID。"""
        return tuple(
            int(item["id"])
            for item in (infos or [])[:10]
            if str(item.get("id") or "").isdigit()
        )

    @staticmethod
    def _build_recognized_media(
        info: dict[str, Any],
        actors: list[dict[str, Any]],
        meta: Optional[MetaBase],
    ) -> MediaInfo:
        """把 Bangumi 详情和演职员投影为统一媒体信息。"""
        enriched = {**info, "actors": actors}
        mediainfo = MediaInfo(bangumi_info=enriched)
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        return mediainfo

    @staticmethod
    def _project_media_list(
        infos: Optional[list[dict[str, Any]]],
    ) -> list[MediaInfo]:
        """把 Bangumi 媒体列表投影为统一媒体信息列表。"""
        return [MediaInfo(bangumi_info=info) for info in infos or []]

    @classmethod
    def _project_search_results(
        cls, meta: MetaBase, infos: Optional[list[dict[str, Any]]]
    ) -> list[MediaInfo]:
        """按标题包含关系过滤 Bangumi 搜索结果并统一投影。"""
        query = meta.name.lower()
        return cls._project_media_list(
            [
                info
                for info in infos or []
                if query in str(info.get("name")).lower()
                or query in str(info.get("name_cn")).lower()
            ]
        )

    @staticmethod
    def _project_people(
        persons: Optional[list[dict[str, Any]]],
    ) -> list[_SchemaMediaPerson]:
        """把 Bangumi 演职员列表投影为统一人物列表。"""
        return [_SchemaMediaPerson(source='bangumi', **person) for person in persons or []]

    def recognize_media(
        self,
        meta: MetaBase = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta: 识别的元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            self.bangumi_info(bangumiid=plan.media_id)
            if plan.media_id is not None
            else self._match_by_meta(plan.require_meta())
        )
        if info:
            actors = self.bangumiapi.credits(info.get("id"))
            mediainfo = self._build_recognized_media(info, actors, plan.meta)
            logger.info(f"{plan.label} Bangumi识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{plan.label} 未匹配到Bangumi媒体信息")

        return None

    async def async_recognize_media(
        self,
        meta: MetaBase = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta: 识别的元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            await self.async_bangumi_info(bangumiid=plan.media_id)
            if plan.media_id is not None
            else await self._async_match_by_meta(plan.require_meta())
        )
        if info:
            actors = await self.bangumiapi.async_credits(info.get("id"))
            mediainfo = self._build_recognized_media(info, actors, plan.meta)
            logger.info(f"{plan.label} Bangumi识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{plan.label} 未匹配到Bangumi媒体信息")

        return None

    @staticmethod
    def _matches_meta(meta: MetaBase, info: dict) -> bool:
        """
        判断Bangumi候选项是否符合标题解析出的类型与年份。

        :param meta: 标题解析元数据
        :param info: Bangumi候选项详情
        :return: 是否符合筛选条件
        """
        if (
            meta.type in {MediaType.MOVIE, MediaType.TV}
            and resolve_bangumi_media_type(info) != meta.type
        ):
            return False
        release_date = info.get("date") or info.get("air_date") or ""
        return not meta.year or not release_date or release_date[:4] == str(meta.year)

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        for media_id in self._candidate_ids(self.bangumiapi.search(meta.name)):
            info = self.bangumiapi.detail(media_id)
            if info and self._matches_meta(meta, info):
                return info
        return None

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        异步搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        infos = await self.bangumiapi.async_search(meta.name)
        for media_id in self._candidate_ids(infos):
            info = await self.bangumiapi.async_detail(media_id)
            if info and self._matches_meta(meta, info):
                return info
        return None

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        return self._project_search_results(
            plan.meta, self.bangumiapi.search(plan.meta.name)
        )

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        infos = await self.bangumiapi.async_search(plan.meta.name)
        return self._project_search_results(plan.meta, infos)

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return self.bangumiapi.detail(bangumiid)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return await self.bangumiapi.async_detail(bangumiid)

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        生成Bangumi来源的NFO内容。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: NFO XML文本
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "bangumi":
            return None
        return self.scraper.get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取Bangumi来源的刮削图片清单。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: 图片文件名与下载地址映射
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "bangumi":
            return None
        return self.scraper.get_metadata_img(mediainfo, season=season, episode=episode)

    def bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送
        """
        return self._project_media_list(self.bangumiapi.calendar())

    async def async_bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送（异步版本）
        """
        infos = await self.bangumiapi.async_calendar()
        return self._project_media_list(infos)

    def bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        return self._project_people(self.bangumiapi.credits(bangumiid))

    async def async_bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表（异步版本）
        :param bangumiid:  BangumiID
        """
        persons = await self.bangumiapi.async_credits(bangumiid)
        return self._project_people(persons)

    def bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self._project_media_list(self.bangumiapi.subjects(bangumiid))

    async def async_bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        根据BangumiID查询推荐电影（异步版本）
        :param bangumiid:  BangumiID
        """
        subjects = await self.bangumiapi.async_subjects(bangumiid)
        return self._project_media_list(subjects)

    def bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取人物详细信息
        :param person_id:  豆瓣人物ID
        """
        personinfo = self.bangumiapi.person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    async def async_bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取人物详细信息（异步版本）
        :param person_id:  豆瓣人物ID
        """
        personinfo = await self.bangumiapi.async_person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    @classmethod
    def _build_person_detail(cls, personinfo: dict) -> _SchemaMediaPerson:
        """
        构造Bangumi人物详情信息。
        :param personinfo: Bangumi人物详情接口返回数据
        :return: 媒体人物信息
        """
        return _SchemaMediaPerson(source='bangumi', **{
            "id": personinfo.get("id"),
            "name": personinfo.get("name"),
            "images": personinfo.get("images"),
            "biography": personinfo.get("summary"),
            "birthday": cls._normalize_optional_string(personinfo.get("birth_day")),
            "gender": personinfo.get("gender")
        })

    @staticmethod
    def _normalize_optional_string(value: object) -> Optional[str]:
        """
        规范化Bangumi接口中可能返回非字符串的可选文本字段。
        :param value: 原始字段值
        :return: 字符串字段值或None
        """
        if value is None:
            return None
        return str(value)

    def bangumi_person_credits(self, person_id: int) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品
        :param person_id:  人物ID
        """
        return self._project_media_list(
            self.bangumiapi.person_credits(person_id=person_id)
        )

    async def async_bangumi_person_credits(self, person_id: int) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        """
        credits_info = await self.bangumiapi.async_person_credits(person_id=person_id)
        return self._project_media_list(credits_info)

    def bangumi_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧
        """
        return self._project_media_list(self.bangumiapi.discover(**kwargs))

    async def async_bangumi_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧（异步版本）
        """
        infos = await self.bangumiapi.async_discover(**kwargs)
        return self._project_media_list(infos)

    def clear_cache(self) -> None:
        """
        清除缓存
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        self.bangumiapi.clear_cache()
        logger.info(f"{self.get_name()}缓存清除完成")
