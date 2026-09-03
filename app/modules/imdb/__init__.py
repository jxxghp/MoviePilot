"""IMDb 原生媒体数据源 Module。"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.domain.scraper import MediaScraperHelper
from app.foundation.text import convert as zhconv_convert
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.context import MediaCredit, MediaImageSet
from app.schemas.media import normalize_media_source
from app.schemas.types import (
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)

from .api import (
    ImdbAka,
    ImdbApi,
    ImdbCredit,
    ImdbEpisode,
    ImdbImage,
    ImdbSeason,
    ImdbTitle,
)


@dataclass(frozen=True, slots=True)
class ImdbConfigSnapshot:
    """IMDb Module 一次配置 generation 使用的网络快照。"""

    proxy: Any


@dataclass(frozen=True, slots=True)
class _ImdbRecognitionPlan:
    """描述一次 IMDb 识别应走显式详情还是标题搜索。"""

    media_id: Optional[str]
    meta: Optional[MetaBase]
    media_type: Optional[MediaType]

    def require_meta(self) -> MetaBase:
        """返回标题搜索计划必有的解析元数据。"""
        if self.meta is None:
            raise RuntimeError("IMDb 标题识别计划缺少解析元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _ImdbCandidatePlan:
    """保存无需别名查询即可命中的候选及后续别名候选。"""

    direct_match: Optional[ImdbTitle]
    alias_candidates: tuple[ImdbTitle, ...]


@dataclass(frozen=True, slots=True)
class _ImdbSearchPlan:
    """保存 IMDb 搜索来源准入结果和有效查询元数据。"""

    enabled: bool
    meta: Optional[MetaBase]


class ImdbModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """提供 IMDb 搜索、识别、详情补全与刮削能力。"""

    auxiliary_media_source = MediaSource.IMDb
    CONFIG_WATCH = {"PROXY_HOST"}
    _IMDB_ID_PATTERN = re.compile(r"^tt\d+$", re.IGNORECASE)
    _MOVIE_TYPES = frozenset({"movie", "tvMovie"})
    _TV_TYPES = frozenset({"tvSeries", "tvMiniSeries", "tvShort", "tvSpecial"})

    imdb_api: Optional[ImdbApi] = None
    scraper: Optional[MediaScraperHelper] = None
    _config = ImdbConfigSnapshot(proxy=None)

    def init_module(self) -> None:
        """按当前代理配置初始化 IMDb 客户端和通用刮削器。"""
        self._config = ImdbConfigSnapshot(proxy=get_runtime_setting('PROXY'))
        self.imdb_api = ImdbApi(proxies=self._config.proxy)
        self.scraper = MediaScraperHelper()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """IMDb 随宿主启动，无需独立模块开关。"""
        return None

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "IMDb"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的媒体识别类型。"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """返回 IMDb 媒体识别子类型。"""
        return MediaRecognizeType.IMDb

    @staticmethod
    def get_priority() -> int:
        """返回模块调度优先级。"""
        return 4

    def stop(self) -> None:
        """释放当前 generation 持有的客户端引用。"""
        if self.imdb_api:
            self.imdb_api.close()
        self.imdb_api = None
        self.scraper = None

    def test(self) -> Tuple[bool, str]:
        """通过最小标题搜索测试 IMDb 数据服务连通性。"""
        if not self.imdb_api:
            return False, "IMDb 模块未初始化"
        results = self.imdb_api.search_titles("The Shawshank Redemption", limit=1)
        return (True, "") if results else (False, "IMDb 数据服务未返回搜索结果")

    @classmethod
    def _media_type(cls, title_type: str) -> MediaType:
        """把 IMDb 标题类型转换为 MoviePilot 媒体类型。"""
        if title_type in cls._MOVIE_TYPES:
            return MediaType.MOVIE
        if title_type in cls._TV_TYPES:
            return MediaType.TV
        return MediaType.UNKNOWN

    @staticmethod
    def _normalize_name(value: Optional[str]) -> str:
        """移除标题标点与空白，生成用于精确比较的稳定文本。"""
        return re.sub(r"[\W_]+", "", value or "", flags=re.UNICODE).casefold()

    @classmethod
    def _name_matches(cls, query: str, names: list[Optional[str]]) -> bool:
        """判断查询标题是否与任一 IMDb 标题或别名精确匹配。"""
        normalized_query = cls._normalize_name(query)
        return bool(
            normalized_query
            and any(
                cls._normalize_name(candidate) == normalized_query
                for candidate in names
                if candidate
            )
        )

    @classmethod
    def _normalize_imdb_id(cls, media_id: object) -> Optional[str]:
        """校验并规范化 IMDb 的 ``tt`` 数字 ID。"""
        value = str(media_id or "").strip().lower()
        return value if cls._IMDB_ID_PATTERN.fullmatch(value) else None

    @classmethod
    def _supports_request_type(
        cls, meta: Optional[MetaBase], mtype: Optional[MediaType]
    ) -> bool:
        """限制 IMDb Module 只处理电影和电视剧请求。"""
        requested_type = mtype or getattr(meta, "type", None)
        return requested_type not in {MediaType.MUSIC, MediaType.MUSIC.value, "music"}

    @classmethod
    def _recognition_plan(
        cls,
        meta: Optional[MetaBase],
        mtype: Optional[MediaType],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
    ) -> Optional[_ImdbRecognitionPlan]:
        """统一校验媒体类型、来源选择和显式 IMDb ID。"""
        if not cls._supports_request_type(meta, mtype):
            return None
        requested_source = normalize_media_source(media_source)
        if media_id is not None:
            normalized_id = cls._normalize_imdb_id(media_id)
            if requested_source != MediaSource.IMDb or not normalized_id:
                return None
            return _ImdbRecognitionPlan(normalized_id, meta, mtype)
        if requested_source not in {None, MediaSource.IMDb}:
            return None
        selected_source = requested_source or normalize_media_source(
            get_runtime_setting('RECOGNIZE_SOURCE')
        )
        if selected_source != MediaSource.IMDb or not meta or not meta.name:
            return None
        return _ImdbRecognitionPlan(None, meta, mtype)

    @staticmethod
    def _search_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSourceSelection],
    ) -> _ImdbSearchPlan:
        """统一决定 IMDb 搜索是否响应以及是否具备查询标题。"""
        enabled = is_media_source_enabled(media_source, MediaSource.IMDb)
        return _ImdbSearchPlan(
            enabled=enabled,
            meta=meta if enabled and meta and meta.name else None,
        )

    @staticmethod
    def _recognize_names(meta: MetaBase) -> list[str]:
        """按中文、简体中文、英文和解析主标题顺序生成识别词。"""
        simplified_name = (
            zhconv_convert(meta.cn_name, "zh-hans") if meta.cn_name else None
        )
        names = [meta.cn_name, simplified_name, meta.en_name, meta.name]
        return list(dict.fromkeys(name for name in names if isinstance(name, str) and name))

    @classmethod
    def _candidate_titles(
        cls,
        titles: list[ImdbTitle],
        mtype: Optional[MediaType],
        year: Optional[str],
    ) -> list[ImdbTitle]:
        """按支持类型、请求类型和年份给 IMDb 候选项排序。"""
        candidates = [
            title
            for title in titles
            if cls._media_type(title.type) in {MediaType.MOVIE, MediaType.TV}
        ]
        if mtype in {MediaType.MOVIE, MediaType.TV}:
            candidates = [
                title for title in candidates if cls._media_type(title.type) == mtype
            ]
        if year and str(year).isdigit():
            requested_year = int(year)
            candidates.sort(
                key=lambda title: (
                    abs((title.start_year or requested_year + 99) - requested_year),
                    -(title.start_year or 0),
                )
            )
        else:
            candidates.sort(key=lambda title: title.start_year or 0, reverse=True)
        return candidates

    @classmethod
    def _candidate_plan(
        cls,
        query: str,
        titles: list[ImdbTitle],
        mtype: Optional[MediaType],
        year: Optional[str],
    ) -> _ImdbCandidatePlan:
        """生成直接标题匹配和最多十个别名回查候选。"""
        candidates = cls._candidate_titles(titles, mtype, year)
        direct_match = next(
            (
                title
                for title in candidates
                if cls._name_matches(
                    query, [title.primary_title, title.original_title]
                )
            ),
            None,
        )
        return _ImdbCandidatePlan(
            direct_match=direct_match,
            alias_candidates=tuple(candidates[:10]) if direct_match is None else (),
        )

    def _pick_title(
        self,
        query: str,
        titles: list[ImdbTitle],
        mtype: Optional[MediaType],
        year: Optional[str],
    ) -> Optional[ImdbTitle]:
        """同步选取标题、原名或别名精确命中的 IMDb 候选项。"""
        plan = self._candidate_plan(query, titles, mtype, year)
        if plan.direct_match:
            return plan.direct_match
        if not self.imdb_api:
            return None
        for title in plan.alias_candidates:
            akas = self.imdb_api.list_akas(title.id)
            if self._name_matches(query, [aka.text for aka in akas]):
                return title
        return None

    async def _async_pick_title(
        self,
        query: str,
        titles: list[ImdbTitle],
        mtype: Optional[MediaType],
        year: Optional[str],
    ) -> Optional[ImdbTitle]:
        """异步选取标题、原名或别名精确命中的 IMDb 候选项。"""
        plan = self._candidate_plan(query, titles, mtype, year)
        if plan.direct_match:
            return plan.direct_match
        if not self.imdb_api:
            return None
        for title in plan.alias_candidates:
            akas = await self.imdb_api.async_list_akas(title.id)
            if self._name_matches(query, [aka.text for aka in akas]):
                return title
        return None

    def _match_by_meta(
        self, meta: MetaBase, mtype: Optional[MediaType]
    ) -> Optional[ImdbTitle]:
        """同步按解析元数据搜索并匹配一个 IMDb 条目。"""
        if not self.imdb_api:
            return None
        requested_type = mtype or meta.type
        for name in self._recognize_names(meta):
            logger.info("正在使用 IMDb 识别：%s ...", name)
            titles = self.imdb_api.search_titles(name)
            title = self._pick_title(name, titles, requested_type, meta.year)
            if title:
                return title
        return None

    async def _async_match_by_meta(
        self, meta: MetaBase, mtype: Optional[MediaType]
    ) -> Optional[ImdbTitle]:
        """异步按解析元数据搜索并匹配一个 IMDb 条目。"""
        if not self.imdb_api:
            return None
        requested_type = mtype or meta.type
        for name in self._recognize_names(meta):
            logger.info("正在使用 IMDb 识别：%s ...", name)
            titles = await self.imdb_api.async_search_titles(name)
            title = await self._async_pick_title(
                name, titles, requested_type, meta.year
            )
            if title:
                return title
        return None

    @staticmethod
    def _person_credit(
        credit: ImdbCredit, *, director: bool = False
    ) -> Optional[dict]:
        """把 IMDb 演职员条目转换为统一演职员摘要。"""
        if not credit.name or not credit.name.display_name:
            return None
        image_url = credit.name.primary_image.url if credit.name.primary_image else None
        media_credit = MediaCredit(
            id=credit.name.id,
            name=credit.name.display_name,
            character=credit.characters[0] if credit.characters else None,
            job="Director" if director else None,
            profile_path=image_url,
            url=(
                f"https://www.imdb.com/name/{credit.name.id}/"
                if credit.name.id
                else None
            ),
            avatar=image_url,
            images=MediaImageSet(large=image_url) if image_url else None,
        )
        return media_credit.model_dump(exclude_none=True)

    @staticmethod
    def _backdrop_url(images: list[ImdbImage]) -> Optional[str]:
        """按剧照、幕后照顺序选择一张 IMDb 背景图。"""
        for image_type in ("still_frame", "behind_the_scenes"):
            if image := next(
                (item for item in images if item.type == image_type and item.url),
                None,
            ):
                return image.url
        return next((item.url for item in images if item.url), None)

    @classmethod
    def _to_media_info(
        cls,
        title: ImdbTitle,
        akas: Optional[list[ImdbAka]] = None,
        credits: Optional[list[ImdbCredit]] = None,
        episodes: Optional[list[ImdbEpisode]] = None,
        seasons: Optional[list[ImdbSeason]] = None,
        images: Optional[list[ImdbImage]] = None,
    ) -> MediaInfo:
        """把 IMDb 详情及关联资源转换为统一媒体信息。"""
        akas = akas or []
        credits = credits or []
        episodes = episodes or []
        seasons = seasons or []
        images = images or []
        media_type = cls._media_type(title.type)
        names = list(
            dict.fromkeys(
                name
                for name in (
                    title.primary_title,
                    title.original_title,
                    *(aka.text for aka in akas),
                )
                if name
            )
        )
        media_info = MediaInfo()
        media_info.media_source = MediaSource.IMDb
        media_info.media_id = title.id
        media_info.imdb_id = title.id
        media_info.type = media_type
        media_info.title = title.primary_title or title.original_title or ""
        media_info.en_title = title.primary_title
        media_info.original_title = title.original_title
        media_info.original_name = title.original_title
        media_info.names = names
        media_info.year = str(title.start_year) if title.start_year else ""
        media_info.overview = title.plot or ""
        media_info.adult = (
            title.is_adult if isinstance(title.is_adult, bool) else None
        )
        media_info.poster_path = title.primary_image.url if title.primary_image else None
        media_info.backdrop_path = cls._backdrop_url(images)
        media_info.genres = [
            {"id": genre, "name": genre} for genre in title.genres
        ]
        media_info.origin_country = [
            item.code for item in title.origin_countries if item.code
        ]
        media_info.production_countries = [
            {"name": item.name or item.code}
            for item in title.origin_countries
            if item.name or item.code
        ]
        media_info.spoken_languages = [
            {"iso_639_1": item.code, "name": item.name or item.code}
            for item in title.spoken_languages
            if item.code
        ]
        if title.spoken_languages:
            media_info.original_language = title.spoken_languages[0].code
        if title.rating:
            media_info.vote_average = title.rating.aggregate_rating
            media_info.vote_count = title.rating.vote_count
        if title.runtime_seconds:
            media_info.runtime = max(1, round(title.runtime_seconds / 60))
            media_info.episode_run_time = [media_info.runtime]

        directors: list[dict] = []
        actors: list[dict] = []
        for credit in credits:
            category = (credit.category or "").upper()
            if category == "DIRECTOR":
                if item := cls._person_credit(credit, director=True):
                    directors.append(item)
            elif category in {"CAST", "ACTOR", "ACTRESS"}:
                if item := cls._person_credit(credit):
                    actors.append(item)
        if not directors:
            directors = [
                item
                for person in title.directors
                if (item := cls._person_credit(ImdbCredit(name=person), director=True))
            ]
        if not actors:
            actors = [
                item
                for person in title.stars
                if (item := cls._person_credit(ImdbCredit(name=person)))
            ]
        media_info.directors = directors[:3]
        media_info.actors = actors[:12]

        season_info: dict[int, dict] = {}
        for season in seasons:
            if not season.season or not season.season.isdigit():
                continue
            season_number = int(season.season)
            season_info[season_number] = {
                "season_number": season_number,
                "episode_count": season.episode_count,
                "name": season.season,
            }
        for episode in episodes:
            if not episode.season or not episode.season.isdigit():
                continue
            season_number = int(episode.season)
            media_info.seasons.setdefault(season_number, []).append(
                episode.episode_number or 0
            )
            if (
                season_number not in media_info.season_years
                and episode.release_date
                and episode.release_date.year
            ):
                media_info.season_years[season_number] = str(episode.release_date.year)
            season_info.setdefault(
                season_number,
                {
                    "season_number": season_number,
                    "episode_count": None,
                    "name": str(season_number),
                },
            )
        media_info.season_info = list(season_info.values())
        if media_type == MediaType.TV:
            media_info.number_of_seasons = len(season_info)
            media_info.number_of_episodes = len(episodes)
        return media_info

    @classmethod
    def _project_search_results(
        cls,
        titles: list[ImdbTitle],
        mtype: Optional[MediaType],
        year: Optional[str],
    ) -> list[MediaInfo]:
        """排序过滤 IMDb 搜索候选并投影为统一媒体信息。"""
        return [
            cls._to_media_info(title)
            for title in cls._candidate_titles(titles, mtype, year)
        ]

    @staticmethod
    def _finish_recognition(
        media_info: MediaInfo, meta: Optional[MetaBase]
    ) -> MediaInfo:
        """保留解析季号并统一记录 IMDb 识别结果。"""
        if meta and meta.begin_season is not None:
            media_info.season = meta.begin_season
        logger.info(
            "IMDb 识别结果：%s %s %s:%s",
            media_info.type.value,
            media_info.title_year,
            media_info.media_source,
            media_info.media_id,
        )
        return media_info

    def _load_media_info(self, title: ImdbTitle) -> MediaInfo:
        """同步补齐一个 IMDb 条目的别名、演职员、剧集和图片。"""
        if not self.imdb_api:
            return self._to_media_info(title)
        details = self.imdb_api.get_title(title.id) or title
        akas = self.imdb_api.list_akas(title.id)
        credits = self.imdb_api.list_credits(title.id)
        images = self.imdb_api.list_images(title.id)
        if self._media_type(details.type) == MediaType.TV:
            episodes = self.imdb_api.list_episodes(title.id)
            seasons = self.imdb_api.list_seasons(title.id)
        else:
            episodes = []
            seasons = []
        return self._to_media_info(
            details,
            akas=akas,
            credits=credits,
            episodes=episodes,
            seasons=seasons,
            images=images,
        )

    async def _async_load_media_info(self, title: ImdbTitle) -> MediaInfo:
        """并发补齐一个 IMDb 条目的别名、演职员、剧集和图片。"""
        if not self.imdb_api:
            return self._to_media_info(title)
        is_tv = self._media_type(title.type) == MediaType.TV
        results = await asyncio.gather(
            self.imdb_api.async_get_title(title.id),
            self.imdb_api.async_list_akas(title.id),
            self.imdb_api.async_list_credits(title.id),
            self.imdb_api.async_list_images(title.id),
            self.imdb_api.async_list_episodes(title.id) if is_tv else asyncio.sleep(0, result=[]),
            self.imdb_api.async_list_seasons(title.id) if is_tv else asyncio.sleep(0, result=[]),
            return_exceptions=True,
        )
        details = results[0] if isinstance(results[0], ImdbTitle) else title
        return self._to_media_info(
            details,
            akas=results[1] if isinstance(results[1], list) else [],
            credits=results[2] if isinstance(results[2], list) else [],
            images=results[3] if isinstance(results[3], list) else [],
            episodes=results[4] if isinstance(results[4], list) else [],
            seasons=results[5] if isinstance(results[5], list) else [],
        )

    def recognize_media(
        self,
        meta: Optional[MetaBase] = None,
        mtype: Optional[MediaType] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        episode_group: Optional[str] = None,
        cache: bool = True,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """按 IMDb 显式身份或标题元数据同步识别影视信息。"""
        del episode_group, cache, kwargs
        if not self.imdb_api:
            return None
        plan = self._recognition_plan(meta, mtype, media_source, media_id)
        if not plan:
            return None
        title = (
            self.imdb_api.get_title(plan.media_id)
            if plan.media_id is not None
            else self._match_by_meta(plan.require_meta(), plan.media_type)
        )
        if not title:
            return None
        return self._finish_recognition(self._load_media_info(title), plan.meta)

    async def async_recognize_media(
        self,
        meta: Optional[MetaBase] = None,
        mtype: Optional[MediaType] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        episode_group: Optional[str] = None,
        cache: bool = True,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """按 IMDb 显式身份或标题元数据异步识别影视信息。"""
        del episode_group, cache, kwargs
        if not self.imdb_api:
            return None
        plan = self._recognition_plan(meta, mtype, media_source, media_id)
        if not plan:
            return None
        title = (
            await self.imdb_api.async_get_title(plan.media_id)
            if plan.media_id is not None
            else await self._async_match_by_meta(plan.require_meta(), plan.media_type)
        )
        if not title:
            return None
        media_info = await self._async_load_media_info(title)
        return self._finish_recognition(media_info, plan.meta)

    def search_medias(
        self,
        meta: MetaBase,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> Optional[list[MediaInfo]]:
        """按请求级来源选择同步搜索 IMDb 影视条目。"""
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not self.imdb_api or not plan.meta:
            return []
        return self._project_search_results(
            self.imdb_api.search_titles(plan.meta.name),
            plan.meta.type,
            plan.meta.year,
        )

    async def async_search_medias(
        self,
        meta: MetaBase,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> Optional[list[MediaInfo]]:
        """按请求级来源选择异步搜索 IMDb 影视条目。"""
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not self.imdb_api or not plan.meta:
            return []
        titles = await self.imdb_api.async_search_titles(plan.meta.name)
        return self._project_search_results(
            titles, plan.meta.type, plan.meta.year
        )

    def clear_cache(self) -> None:
        """清理 IMDb 请求缓存并记录统一模块日志。"""
        if not self.imdb_api:
            return
        logger.info("开始清除 IMDb 缓存 ...")
        self.imdb_api.clear_cache()
        logger.info("IMDb 缓存清除完成")

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """为 IMDb 媒体信息同步补充缺失的背景图片。"""
        if mediainfo.media_source != MediaSource.IMDb or not self.imdb_api:
            return None
        if mediainfo.backdrop_path:
            return mediainfo
        imdb_id = self._normalize_imdb_id(mediainfo.media_id)
        if not imdb_id:
            return None
        mediainfo.backdrop_path = self._backdrop_url(
            self.imdb_api.list_images(imdb_id)
        )
        return mediainfo

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """为 IMDb 媒体信息异步补充缺失的背景图片。"""
        if mediainfo.media_source != MediaSource.IMDb or not self.imdb_api:
            return None
        if mediainfo.backdrop_path:
            return mediainfo
        imdb_id = self._normalize_imdb_id(mediainfo.media_id)
        if not imdb_id:
            return None
        mediainfo.backdrop_path = self._backdrop_url(
            await self.imdb_api.async_list_images(imdb_id)
        )
        return mediainfo

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs,
    ) -> Optional[str]:
        """生成 IMDb 来源的 NFO 元数据文本。"""
        del kwargs
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != MediaSource.IMDb.value:
            return None
        if not self.scraper:
            return None
        return self.scraper.get_metadata_nfo(
            mediainfo, season=season, episode=episode
        )

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """生成 IMDb 来源的图片文件名与下载地址映射。"""
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != MediaSource.IMDb.value:
            return None
        if not self.scraper:
            return None
        return self.scraper.get_metadata_img(
            mediainfo, season=season, episode=episode
        )
