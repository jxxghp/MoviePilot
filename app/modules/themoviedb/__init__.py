import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, Generator, List, Optional, Tuple, Union, cast

import cn2an

from app.adapters.network.http import RequestUtils
from app.domain.classification.evaluator import read_fact
from app.domain.classification.facts import build_classification_facts
from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled, is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.foundation.text import convert as zhconv_convert
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.themoviedb.cache import TmdbCache
from app.modules.themoviedb.category import CategoryHelper as CategoryHelper
from app.modules.themoviedb.scraper import TmdbScraper
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbConnectionError
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.category import (
    ClassificationEnrichmentMatch,
    ClassificationEnrichmentRequest,
    ClassificationEnrichmentResponse,
)
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.media import normalize_media_source
from app.schemas.tmdb import TmdbEpisode as _SchemaTmdbEpisode
from app.schemas.tmdb import TmdbSeason as _SchemaTmdbSeason
from app.schemas.types import (
    MediaImageType,
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TmdbData = Dict[str, Any]
_TmdbDataList = List[_TmdbData]


@dataclass(frozen=True)
class _RecognizePlan:
    """保存同步与异步 TMDB 识别入口共用的请求决策。"""

    meta: Optional[MetaBase]
    mtype: Optional[MediaType]
    tmdbid: Optional[int]
    episode_group: Optional[str]
    use_cache: bool


class _RecognizeAction(Enum):
    """标识 TMDB 识别纯状态机交给同步或异步外壳执行的 I/O 动作。"""

    LOAD_CACHE = auto()
    LOAD_GROUP = auto()
    LOOKUP_IDENTITY = auto()
    SEARCH_NAME = auto()
    LOAD_DETAILS = auto()
    SAVE_CACHE = auto()
    BUILD_RESULT = auto()
    LOG_CACHE = auto()
    LOG_FAILURE = auto()
    LOG_MISS = auto()


@dataclass(frozen=True)
class _RecognizeStep:
    """描述 TMDB 识别状态机的一次 I/O 请求及其稳定调用参数。"""

    action: _RecognizeAction
    kwargs: Dict[str, Any]


@dataclass(frozen=True)
class _RecognizeLookup:
    """承载显式身份查询结果及连接失败状态，供纯状态机统一决策。"""

    info: Optional[_TmdbData]
    connection_error: bool = False


@dataclass(frozen=True)
class _MatchStep:
    """描述一次按名称匹配调用，I/O 外壳只负责选择同步或异步客户端。"""

    name: str
    mtype: Optional[MediaType] = None
    year: Optional[str] = None
    season_year: Optional[str] = None
    season_number: Optional[int] = None
    include_year: bool = False
    include_season: bool = False
    include_group_seasons: bool = False
    multi: bool = False


@dataclass(frozen=True)
class _MediaSearchPlan:
    """描述媒体搜索所需的 TMDB 查询组合。"""

    name: str
    year: Any
    search_movies: bool
    search_tvs: bool
    search_multi: bool
    sort_combined: bool


@dataclass(frozen=True)
class _ImageQuery:
    """保存同步与异步图片客户端共用的查询参数。"""

    tmdbid: int
    original_language: Optional[str]
    movie: bool


class TheMovieDbModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """
    TMDB媒体信息匹配
    """

    CONFIG_WATCH = {"PROXY_HOST", "TMDB_API_DOMAIN", "TMDB_API_KEY", "TMDB_LOCALE"}
    auxiliary_media_source = MediaSource.TMDB

    # 元数据缓存
    cache: TmdbCache = None
    # TMDB
    tmdb: TmdbApi = None
    # 刮削器
    scraper: TmdbScraper = None

    def init_module(self) -> None:
        self.cache = TmdbCache()
        self.tmdb = TmdbApi()
        self.scraper = TmdbScraper()

    @staticmethod
    def get_name() -> str:
        return "TheMovieDb"

    @staticmethod
    def get_classification_enrichment_sources() -> tuple[MediaSource, ...]:
        """声明本模块只能以 TMDB 来源补充标准分类事实。"""
        return (MediaSource.TMDB,)

    def get_media_classification_facts(
        self,
        request: ClassificationEnrichmentRequest,
    ) -> ClassificationEnrichmentResponse | None:
        """通过请求中已知 TMDB ID 补充影视标准事实，不修改主媒体身份。"""
        if request.media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
            return None
        raw_tmdb_id = request.external_ids.get(MediaSource.TMDB.value)
        if not raw_tmdb_id:
            return None
        try:
            tmdb_id = int(raw_tmdb_id)
            media_type = MediaType(request.media_type)
        except TypeError, ValueError:
            return None
        info = self.tmdb_info(tmdbid=tmdb_id, mtype=media_type)
        if not info:
            return None
        facts = build_classification_facts(MediaInfo(tmdb_info=info))
        supplied = {}
        for field_id in request.missing_fields:
            value, missing = read_fact(facts, field_id)
            if not missing:
                supplied[field_id] = value
        return ClassificationEnrichmentResponse(
            media_source=MediaSource.TMDB.value,
            match=ClassificationEnrichmentMatch(
                kind="external_id",
                media_source=MediaSource.TMDB.value,
                media_id=raw_tmdb_id,
            ),
            facts=supplied,
        )

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
        return MediaRecognizeType.TMDB

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 1

    def stop(self) -> None:
        """停止模块"""
        # 缓存持久化失败不能阻断 HTTP 客户端关闭
        try:
            self.cache.save()
        finally:
            self.tmdb.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils(ua=get_runtime_setting("NORMAL_USER_AGENT"), proxies=get_runtime_setting("PROXY")).get_res(
            f"https://{get_runtime_setting('TMDB_API_DOMAIN')}/3/movie/550?api_key={get_runtime_setting('TMDB_API_KEY')}"
        )
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接 {get_runtime_setting('TMDB_API_DOMAIN')}，错误码：{ret.status_code}"
        return False, f"{get_runtime_setting('TMDB_API_DOMAIN')} 网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def _validate_recognize_params(
        meta: Optional[MetaBase],
        tmdbid: Optional[int],
        media_source: Optional[MediaSource] = None,
    ) -> bool:
        """
        验证识别参数

        :param meta: 标题解析元数据
        :param tmdbid: TMDB ID
        :param media_source: 请求级识别数据源
        :return: 参数是否可用于TMDB识别
        """
        if not tmdbid and not meta:
            return False

        selected_source = normalize_media_source(media_source or get_runtime_setting("RECOGNIZE_SOURCE"))
        if meta and not tmdbid and selected_source != MediaSource.TMDB:
            return False

        if meta and not meta.name and not tmdbid:
            logger.warn("识别媒体信息时未提供元数据名称")
            return False

        return True

    @classmethod
    def _build_recognize_plan(
        cls,
        meta: Optional[MetaBase],
        mtype: Optional[MediaType],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        episode_group: Optional[str],
        cache: Optional[bool],
    ) -> Optional[_RecognizePlan]:
        """统一验证识别身份并生成不含客户端状态的执行计划。"""
        if mtype == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.TMDB:
            return None
        if media_id is not None and (media_source != MediaSource.TMDB or not str(media_id).isdigit()):
            return None
        tmdbid = int(media_id) if media_id is not None else None
        if not cls._validate_recognize_params(meta, tmdbid, media_source):
            return None
        return _RecognizePlan(
            meta=meta,
            mtype=mtype,
            tmdbid=tmdbid,
            episode_group=episode_group,
            use_cache=bool(cache),
        )

    def _load_recognize_cache(self, plan: _RecognizePlan) -> _TmdbData:
        """按统一计划投影元数据身份并读取识别缓存。"""
        if not plan.meta:
            return {}
        if plan.mtype:
            plan.meta.type = plan.mtype
        if plan.tmdbid:
            plan.meta.media_source = MediaSource.TMDB
            plan.meta.media_id = str(plan.tmdbid)
        return self.cache.get(plan.meta) if plan.use_cache else {}

    @staticmethod
    def _log_recognize_lookup_failure(plan: _RecognizePlan, connection_error: bool) -> None:
        """按统一语义记录确定缺失、连接失败和无有效输入三类结果。"""
        if connection_error:
            logger.error(f"tmdb_id:{plan.tmdbid} 连接TheMovieDb失败，无法完成识别，请检查网络连接后重试")
        elif plan.tmdbid:
            logger.warn(f"tmdb_id:{plan.tmdbid} 无法确定媒体类型，识别失败")
        else:
            logger.error("识别媒体信息时未提供元数据或唯一且有效的tmdbid")

    def _save_recognize_cache(self, plan: _RecognizePlan, info: Optional[_TmdbData]) -> None:
        """将同步与异步查询结果写入同一识别缓存合同。"""
        if plan.meta:
            self.cache.update(plan.meta, cast(_TmdbData, info))

    @staticmethod
    def _prepare_search_names(meta: MetaBase) -> List[str]:
        """
        准备搜索名称列表
        """
        # 简体名称
        zh_name = zhconv_convert(meta.cn_name, "zh-hans") if meta.cn_name else None
        # 使用中英文名分别识别，去重去空，但要保持顺序
        return list(dict.fromkeys([k for k in [meta.cn_name, zh_name, meta.en_name] if k]))

    @staticmethod
    def _fill_group_season_info(mediainfo: MediaInfo, episode_group: Optional[str], group_seasons: List[dict]) -> None:
        """
        将指定剧集组的季、集、年份信息写入 MediaInfo。
        """
        seasons = {}
        season_info = []
        season_years = {}
        for group_season in group_seasons:
            # 季
            season = group_season.get("order")
            # 集列表
            episodes = group_season.get("episodes")
            if not episodes:
                continue
            seasons[season] = [ep.get("episode_number") for ep in episodes]
            season_info.append(group_season)
            # 当前季第一集时间
            first_date = episodes[0].get("air_date")
            if first_date and _DATE_RE.match(first_date):
                season_years[season] = str(first_date).split("-")[0]
        # 每季集清单
        if seasons:
            mediainfo.seasons = seasons
            mediainfo.number_of_seasons = len(seasons)
        # 每季集详情
        if season_info:
            mediainfo.season_info = season_info
        # 每季年份
        if season_years:
            mediainfo.season_years = season_years
        # 所有剧集组
        mediainfo.episode_group = episode_group
        mediainfo.episode_groups = group_seasons

    @staticmethod
    def _build_search_medias_result(meta: MetaBase, results: Optional[List[dict]]) -> List[MediaInfo]:
        """
        构建搜索结果，并沿用原有逻辑把搜索词中的季写入电视剧标题中。
        """
        if not results:
            return []
        medias = [MediaInfo(tmdb_info=info) for info in results]
        if meta.begin_season is not None:
            # 小写数据转大写
            season_str = cn2an.an2cn(meta.begin_season, "low")
            for media in medias:
                if media.type == MediaType.TV:
                    media.title = f"{media.title} 第{season_str}季"
                    media.season = meta.begin_season
        return medias

    @staticmethod
    def _build_media_search_plan(
        meta: MetaBase, media_source: Optional[MediaSourceSelection]
    ) -> Optional[_MediaSearchPlan]:
        """统一选择媒体搜索接口及组合结果排序策略。"""
        if not is_media_source_enabled(media_source, MediaSource.TMDB):
            return None
        if not meta.name:
            return _MediaSearchPlan(
                name="",
                year=meta.year,
                search_movies=False,
                search_tvs=False,
                search_multi=False,
                sort_combined=False,
            )
        if meta.type == MediaType.UNKNOWN and not meta.year:
            return _MediaSearchPlan(
                name=meta.name,
                year=meta.year,
                search_movies=False,
                search_tvs=False,
                search_multi=True,
                sort_combined=False,
            )
        search_movies = meta.type in (MediaType.UNKNOWN, MediaType.MOVIE)
        search_tvs = meta.type not in (MediaType.MOVIE,)
        return _MediaSearchPlan(
            name=meta.name,
            year=meta.year,
            search_movies=search_movies,
            search_tvs=search_tvs,
            search_multi=False,
            sort_combined=meta.type == MediaType.UNKNOWN,
        )

    @staticmethod
    def _merge_media_search_results(
        plan: _MediaSearchPlan,
        movie_results: Optional[_TmdbDataList] = None,
        tv_results: Optional[_TmdbDataList] = None,
        multi_results: Optional[_TmdbDataList] = None,
    ) -> _TmdbDataList:
        """按搜索计划合并客户端结果，并稳定保持原有日期倒序。"""
        if plan.search_multi:
            return list(multi_results or [])
        results = list(movie_results or [])
        results.extend(tv_results or [])
        if plan.sort_combined:
            results.sort(
                key=lambda item: item.get("release_date") or item.get("first_air_date") or "0000-00-00",
                reverse=True,
            )
        return results

    def _safe_get_info_by_type(self, mtype: MediaType, tmdbid: int) -> Tuple[Optional[dict], bool]:
        """
        查询指定类型的媒体详情，将"确认TMDB连接失败"与"确认查无此项"区分开。

        :param mtype: 媒体类型：电影或电视剧
        :param tmdbid: TMDB的ID
        :return: (媒体信息或None, 本次查询是否因TMDB连接失败而没有得到确定结果)
        """
        try:
            return self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid, raise_on_connection_error=True), False
        except TMDbConnectionError:
            return None, True

    async def _async_safe_get_info_by_type(self, mtype: MediaType, tmdbid: int) -> Tuple[Optional[dict], bool]:
        """
        查询指定类型的媒体详情，将"确认TMDB连接失败"与"确认查无此项"区分开（异步版本）
        """
        try:
            return await self.tmdb.async_get_info(mtype=mtype, tmdbid=tmdbid, raise_on_connection_error=True), False
        except TMDbConnectionError:
            return None, True

    @classmethod
    def _resolve_tmdbid_candidates(
        cls,
        tmdbid: int,
        meta: Optional[MetaBase],
        info_tv: Optional[_TmdbData],
        tv_conn_error: bool,
        info_movie: Optional[_TmdbData],
        movie_conn_error: bool,
    ) -> Optional[_TmdbData]:
        """统一解释电影、电视剧两路详情结果并执行元数据消歧。"""
        if info_tv and info_movie:
            result = cls._disambiguate_by_meta(info_tv, info_movie, meta)
            if result:
                return result
            logger.warn(f"无法判断tmdb_id:{tmdbid} 是电影还是电视剧")
            return None
        if info_tv or info_movie:
            return info_tv or info_movie
        if tv_conn_error or movie_conn_error:
            raise TMDbConnectionError(f"连接TheMovieDb失败，无法确认tmdb_id:{tmdbid} 的媒体类型")
        return None

    def _get_info_by_tmdbid(self, tmdbid: int, mtype: Optional[MediaType], meta: Optional[MetaBase]) -> Optional[dict]:
        """
        根据tmdbid查询媒体信息，当类型未知且同时存在电影和电视剧时，通过元数据消歧

        :raises TMDbConnectionError: 电影、电视剧两路查询都没有得到确定结果，且至少一路
            是因TMDB连接失败导致的，此时不能断言"条目不存在"，交由上层报网络故障
        """
        if mtype:
            return self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid, raise_on_connection_error=True)
        # 类型未知，分别查询电影和电视剧；每一路的连接失败要单独识别，
        # 避免一路瞬时抖动掩盖另一路已经得到的确定结果
        info_tv, tv_conn_error = self._safe_get_info_by_type(MediaType.TV, tmdbid)
        info_movie, movie_conn_error = self._safe_get_info_by_type(MediaType.MOVIE, tmdbid)
        return self._resolve_tmdbid_candidates(tmdbid, meta, info_tv, tv_conn_error, info_movie, movie_conn_error)

    async def _async_get_info_by_tmdbid(
        self, tmdbid: int, mtype: Optional[MediaType], meta: Optional[MetaBase]
    ) -> Optional[dict]:
        """
        根据tmdbid查询媒体信息，当类型未知且同时存在电影和电视剧时，通过元数据消歧（异步版本）

        :raises TMDbConnectionError: 电影、电视剧两路查询都没有得到确定结果，且至少一路
            是因TMDB连接失败导致的，此时不能断言"条目不存在"，交由上层报网络故障
        """
        if mtype:
            return await self.tmdb.async_get_info(mtype=mtype, tmdbid=tmdbid, raise_on_connection_error=True)
        # 类型未知，分别查询电影和电视剧；每一路的连接失败要单独识别，
        # 避免一路瞬时抖动掩盖另一路已经得到的确定结果
        info_tv, tv_conn_error = await self._async_safe_get_info_by_type(MediaType.TV, tmdbid)
        info_movie, movie_conn_error = await self._async_safe_get_info_by_type(MediaType.MOVIE, tmdbid)
        return self._resolve_tmdbid_candidates(tmdbid, meta, info_tv, tv_conn_error, info_movie, movie_conn_error)

    @staticmethod
    def _disambiguate_by_meta(info_tv: dict, info_movie: dict, meta: Optional[MetaBase]) -> Optional[dict]:
        """
        通过元数据（标题、年份、类型）对同tmdbid的电影和电视剧进行消歧
        """
        if not meta:
            return None

        def _collect_titles(info: dict) -> set:
            titles = set()
            for key in ("title", "name", "original_title", "original_name"):
                if info.get(key):
                    titles.add(info[key])
            for name in info.get("names") or []:
                titles.add(name)
            return titles

        def _match_score(info: dict) -> int:
            score = 0
            # 标题匹配
            titles = _collect_titles(info)
            meta_names = [n for n in [meta.cn_name, meta.en_name] if n]
            for meta_name in meta_names:
                if any(meta_name in t or t in meta_name for t in titles):
                    score += 2
                    break
            # 年份匹配
            if meta.year:
                release_date = info.get("release_date") or info.get("first_air_date") or ""
                if release_date and release_date[:4] == meta.year:
                    score += 1
            return score

        score_tv = _match_score(info_tv)
        score_movie = _match_score(info_movie)

        if score_tv > score_movie:
            logger.info(f"通过元数据消歧，tmdb_id:{info_tv.get('id')} 识别为电视剧")
            return info_tv
        elif score_movie > score_tv:
            logger.info(f"通过元数据消歧，tmdb_id:{info_movie.get('id')} 识别为电影")
            return info_movie

        # 评分相同时参考meta.type
        if meta.type == MediaType.TV:
            logger.info(f"通过媒体类型提示消歧，tmdb_id:{info_tv.get('id')} 识别为电视剧")
            return info_tv
        elif meta.type == MediaType.MOVIE:
            logger.info(f"通过媒体类型提示消歧，tmdb_id:{info_movie.get('id')} 识别为电影")
            return info_movie

        return None

    @staticmethod
    def _build_match_plan(name: str, meta: MetaBase) -> Tuple[_MatchStep, ...]:
        """生成名称识别的有序回退计划，确保同步与异步分支完全一致。"""
        if meta.type == MediaType.UNKNOWN and not meta.year:
            return (_MatchStep(name=name, multi=True),)
        if meta.type == MediaType.TV:
            return (
                _MatchStep(
                    name=name,
                    year=meta.year,
                    mtype=meta.type,
                    season_year=meta.year,
                    season_number=meta.begin_season,
                    include_year=True,
                    include_season=True,
                    include_group_seasons=True,
                ),
                _MatchStep(name=name, mtype=meta.type),
            )
        return (
            _MatchStep(
                name=name,
                year=meta.year,
                mtype=MediaType.MOVIE,
                include_year=True,
            ),
            _MatchStep(
                name=name,
                year=meta.year,
                mtype=MediaType.TV,
                include_year=True,
                include_group_seasons=True,
            ),
            _MatchStep(name=name, multi=True),
        )

    @staticmethod
    def _match_kwargs(step: _MatchStep, group_seasons: _TmdbDataList) -> Dict[str, Any]:
        """把匹配步骤转换为客户端参数，并仅在原合同要求时携带剧集组。"""
        kwargs: Dict[str, Any] = {"name": step.name}
        if step.include_year:
            kwargs["year"] = step.year
        if step.mtype is not None:
            kwargs["mtype"] = step.mtype
        if step.include_season:
            kwargs["season_year"] = step.season_year
            kwargs["season_number"] = step.season_number
        if step.include_group_seasons:
            kwargs["group_seasons"] = group_seasons
        return kwargs

    @staticmethod
    def _log_match_start(name: str, meta: MetaBase) -> None:
        """记录名称匹配开始信息，避免双入口文案漂移。"""
        if meta.begin_season is not None:
            logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
        else:
            logger.info(f"正在识别 {name} ...")

    def _search_by_name(self, name: str, meta: MetaBase, group_seasons: _TmdbDataList) -> Optional[_TmdbData]:
        """
        根据名称搜索媒体信息
        """
        self._log_match_start(name, meta)
        for step in self._build_match_plan(name, meta):
            if step.multi:
                info = self.tmdb.match_multi(name=step.name)
            else:
                info = self.tmdb.match(**self._match_kwargs(step, group_seasons))
            if info:
                return info
        return None

    async def _async_search_by_name(
        self, name: str, meta: MetaBase, group_seasons: _TmdbDataList
    ) -> Optional[_TmdbData]:
        """
        根据名称搜索媒体信息（异步版本）
        """
        self._log_match_start(name, meta)
        for step in self._build_match_plan(name, meta):
            if step.multi:
                info = await self.tmdb.async_match_multi(name=step.name)
            else:
                info = await self.tmdb.async_match(**self._match_kwargs(step, group_seasons))
            if info:
                return info
        return None

    @classmethod
    def _prepare_episode_group_queries(
        cls,
        mediainfo: MediaInfo,
        episode_group: Optional[str],
        group_seasons: _TmdbDataList,
    ) -> List[str]:
        """应用已知剧集组并返回仍需查询年份的分组 ID。"""
        if mediainfo.type != MediaType.TV or not mediainfo.episode_groups:
            return []
        if group_seasons:
            cls._fill_group_season_info(mediainfo, episode_group, group_seasons)
            return []
        group_ids: List[str] = []
        for group in mediainfo.episode_groups:
            group_id = group.get("id")
            if group.get("type") == 6 and group_id:
                group_ids.append(cast(str, group_id))
        return group_ids

    @staticmethod
    def _collect_group_season_years(
        group_seasons: _TmdbDataList,
    ) -> Dict[int, str]:
        """从已获取的剧集组详情中提取每季首播年份。"""
        season_years: Dict[int, str] = {}
        for group_season in group_seasons:
            season = group_season.get("order")
            episodes = group_season.get("episodes")
            if not episodes:
                continue
            first_date = episodes[0].get("air_date")
            if first_date and _DATE_RE.match(first_date):
                season_years[cast(int, season)] = str(first_date).split("-")[0]
        return season_years

    @staticmethod
    def _apply_group_season_years(mediainfo: MediaInfo, season_years: Dict[int, str]) -> MediaInfo:
        """把聚合后的剧集组年份写回媒体结果。"""
        if season_years:
            mediainfo.season_years = season_years
        return mediainfo

    def _process_episode_groups(
        self, mediainfo: MediaInfo, episode_group: Optional[str], group_seasons: _TmdbDataList
    ) -> MediaInfo:
        """
        处理剧集组信息
        """
        season_years = {}
        for group_id in self._prepare_episode_group_queries(mediainfo, episode_group, group_seasons):
            fetched_seasons = self.tmdb.get_tv_group_seasons(group_id)
            if fetched_seasons:
                season_years.update(self._collect_group_season_years(fetched_seasons))
        return self._apply_group_season_years(mediainfo, season_years)

    async def _async_process_episode_groups(
        self, mediainfo: MediaInfo, episode_group: Optional[str], group_seasons: _TmdbDataList
    ) -> MediaInfo:
        """
        处理剧集组信息（异步版本）
        """
        season_years = {}
        for group_id in self._prepare_episode_group_queries(mediainfo, episode_group, group_seasons):
            fetched_seasons = await self.tmdb.async_get_tv_group_seasons(group_id)
            if fetched_seasons:
                season_years.update(self._collect_group_season_years(fetched_seasons))
        return self._apply_group_season_years(mediainfo, season_years)

    def _build_media_info_base(self, info: _TmdbData, meta: Optional[MetaBase], tmdbid: Optional[int]) -> MediaInfo:
        """统一完成 TMDB 详情到 MediaInfo 的标准投影与结果日志。"""
        mediainfo = MediaInfo(tmdb_info=info)

        if meta:
            logger.info(f"{meta.name} TMDB识别结果：{mediainfo.type.value} {mediainfo.title_year} {mediainfo.tmdb_id}")
        else:
            logger.info(f"{tmdbid} TMDB识别结果：{mediainfo.type.value} {mediainfo.title_year}")
        return mediainfo

    def _build_media_info_result(
        self,
        info: _TmdbData,
        meta: MetaBase,
        tmdbid: Optional[int],
        episode_group: Optional[str],
        group_seasons: _TmdbDataList,
    ) -> MediaInfo:
        """构建同步识别结果并补充剧集组信息。"""
        mediainfo = self._build_media_info_base(info, meta, tmdbid)

        # 处理剧集组信息
        return self._process_episode_groups(mediainfo, episode_group, group_seasons)

    async def _async_build_media_info_result(
        self,
        info: _TmdbData,
        meta: MetaBase,
        tmdbid: Optional[int],
        episode_group: Optional[str],
        group_seasons: _TmdbDataList,
    ) -> MediaInfo:
        """构建异步识别结果并补充剧集组信息。"""
        mediainfo = self._build_media_info_base(info, meta, tmdbid)

        # 处理剧集组信息
        return await self._async_process_episode_groups(mediainfo, episode_group, group_seasons)

    @classmethod
    def _recognize_steps(cls, plan: _RecognizePlan) -> Generator[_RecognizeStep, Any, Optional[MediaInfo]]:
        """生成双 ABI 共用的 TMDB 识别状态机，仅把实际 I/O 留给入口外壳。"""
        cache_info = yield _RecognizeStep(
            action=_RecognizeAction.LOAD_CACHE,
            kwargs={"plan": plan},
        )
        group_seasons: _TmdbDataList = []
        if plan.episode_group:
            group_seasons = yield _RecognizeStep(
                action=_RecognizeAction.LOAD_GROUP,
                kwargs={"group_id": plan.episode_group},
            )

        cache_hit = bool(cache_info and plan.use_cache)
        info: Optional[_TmdbData]
        if not cache_hit:
            lookup = _RecognizeLookup(info=None)
            if plan.tmdbid:
                lookup = yield _RecognizeStep(
                    action=_RecognizeAction.LOOKUP_IDENTITY,
                    kwargs={
                        "tmdbid": plan.tmdbid,
                        "mtype": plan.mtype,
                        "meta": plan.meta,
                    },
                )
            info = lookup.info
            if not info and plan.meta and not plan.tmdbid:
                for name in cls._prepare_search_names(plan.meta):
                    info = yield _RecognizeStep(
                        action=_RecognizeAction.SEARCH_NAME,
                        kwargs={
                            "name": name,
                            "meta": plan.meta,
                            "group_seasons": group_seasons,
                        },
                    )
                    if info:
                        break
                if info and not info.get("genres"):
                    info = yield _RecognizeStep(
                        action=_RecognizeAction.LOAD_DETAILS,
                        kwargs={
                            "mtype": info.get("media_type"),
                            "tmdbid": info.get("id"),
                        },
                    )
            elif not info:
                yield _RecognizeStep(
                    action=_RecognizeAction.LOG_FAILURE,
                    kwargs={
                        "plan": plan,
                        "connection_error": lookup.connection_error,
                    },
                )
                return None
            yield _RecognizeStep(
                action=_RecognizeAction.SAVE_CACHE,
                kwargs={"plan": plan, "info": info},
            )
        else:
            yield _RecognizeStep(
                action=_RecognizeAction.LOG_CACHE,
                kwargs={"plan": plan, "cache_info": cache_info},
            )
            if cache_info.get("title"):
                info = yield _RecognizeStep(
                    action=_RecognizeAction.LOAD_DETAILS,
                    kwargs={
                        "mtype": cache_info.get("type"),
                        "tmdbid": cache_info.get("id"),
                    },
                )
            else:
                info = None

        if info:
            mediainfo = yield _RecognizeStep(
                action=_RecognizeAction.BUILD_RESULT,
                kwargs={
                    "info": info,
                    "meta": plan.meta,
                    "tmdbid": plan.tmdbid,
                    "episode_group": plan.episode_group,
                    "group_seasons": group_seasons,
                },
            )
            mediainfo.recognize_cache_hit = cache_hit
            return cast(MediaInfo, mediainfo)

        yield _RecognizeStep(
            action=_RecognizeAction.LOG_MISS,
            kwargs={"target": plan.meta.name if plan.meta else plan.tmdbid},
        )
        return None

    def _run_recognize_steps(self, plan: _RecognizePlan) -> Optional[MediaInfo]:
        """用同步 TMDB 客户端驱动共享识别状态机。"""
        steps = self._recognize_steps(plan)
        result: Any = None
        while True:
            try:
                step = steps.send(result)
            except StopIteration as completed:
                return cast(Optional[MediaInfo], completed.value)
            if step.action == _RecognizeAction.LOAD_CACHE:
                result = self._load_recognize_cache(**step.kwargs)
            elif step.action == _RecognizeAction.LOAD_GROUP:
                result = self.tmdb.get_tv_group_seasons(**step.kwargs)
            elif step.action == _RecognizeAction.LOOKUP_IDENTITY:
                try:
                    info = self._get_info_by_tmdbid(**step.kwargs)
                    result = _RecognizeLookup(info=info)
                except TMDbConnectionError as err:
                    logger.error(f"tmdb_id:{plan.tmdbid} {err}")
                    result = _RecognizeLookup(info=None, connection_error=True)
            elif step.action == _RecognizeAction.SEARCH_NAME:
                result = self._search_by_name(**step.kwargs)
            elif step.action == _RecognizeAction.LOAD_DETAILS:
                result = self.tmdb.get_info(**step.kwargs)
            elif step.action == _RecognizeAction.SAVE_CACHE:
                self._save_recognize_cache(**step.kwargs)
                result = None
            elif step.action == _RecognizeAction.BUILD_RESULT:
                result = self._build_media_info_result(**step.kwargs)
            else:
                self._run_common_recognize_step(step)
                result = None

    async def _async_run_recognize_steps(self, plan: _RecognizePlan) -> Optional[MediaInfo]:
        """用异步 TMDB 客户端驱动共享识别状态机。"""
        steps = self._recognize_steps(plan)
        result: Any = None
        while True:
            try:
                step = steps.send(result)
            except StopIteration as completed:
                return cast(Optional[MediaInfo], completed.value)
            if step.action == _RecognizeAction.LOAD_CACHE:
                result = self._load_recognize_cache(**step.kwargs)
            elif step.action == _RecognizeAction.LOAD_GROUP:
                result = await self.tmdb.async_get_tv_group_seasons(**step.kwargs)
            elif step.action == _RecognizeAction.LOOKUP_IDENTITY:
                try:
                    info = await self._async_get_info_by_tmdbid(**step.kwargs)
                    result = _RecognizeLookup(info=info)
                except TMDbConnectionError as err:
                    logger.error(f"tmdb_id:{plan.tmdbid} {err}")
                    result = _RecognizeLookup(info=None, connection_error=True)
            elif step.action == _RecognizeAction.SEARCH_NAME:
                result = await self._async_search_by_name(**step.kwargs)
            elif step.action == _RecognizeAction.LOAD_DETAILS:
                result = await self.tmdb.async_get_info(**step.kwargs)
            elif step.action == _RecognizeAction.SAVE_CACHE:
                self._save_recognize_cache(**step.kwargs)
                result = None
            elif step.action == _RecognizeAction.BUILD_RESULT:
                result = await self._async_build_media_info_result(**step.kwargs)
            else:
                self._run_common_recognize_step(step)
                result = None

    def _run_common_recognize_step(self, step: _RecognizeStep) -> None:
        """执行不依赖 TMDB 客户端 ABI 的缓存和结果日志动作。"""
        if step.action == _RecognizeAction.LOG_CACHE:
            plan = cast(_RecognizePlan, step.kwargs["plan"])
            cache_info = cast(_TmdbData, step.kwargs["cache_info"])
            meta = plan.meta
            target = meta.name if meta else plan.tmdbid
            if cache_info.get("title"):
                logger.info(f"{target} 使用TMDB识别缓存：{cache_info.get('title')}")
            else:
                logger.info(f"{target} 使用TMDB识别缓存：无法识别")
        elif step.action == _RecognizeAction.LOG_FAILURE:
            self._log_recognize_lookup_failure(**step.kwargs)
        else:
            logger.info(f"{step.kwargs['target']} 未匹配到TMDB媒体信息")

    def recognize_media(
        self,
        meta: MetaBase = None,
        mtype: MediaType = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        episode_group: Optional[str] = None,
        cache: Optional[bool] = True,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型
        :param media_source: 媒体来源
        :param media_id: 媒体来源原生ID
        :param episode_group:  剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._build_recognize_plan(meta, mtype, media_source, media_id, episode_group, cache)
        if not plan:
            return None
        return self._run_recognize_steps(plan)

    async def async_recognize_media(
        self,
        meta: MetaBase = None,
        mtype: MediaType = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        episode_group: Optional[str] = None,
        cache: Optional[bool] = True,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型
        :param media_source: 媒体来源
        :param media_id: 媒体来源原生ID
        :param episode_group:  剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._build_recognize_plan(meta, mtype, media_source, media_id, episode_group, cache)
        if not plan:
            return None
        return await self._async_run_recognize_steps(plan)

    def match_tmdbinfo(
        self, name: str, mtype: MediaType = None, year: Optional[str] = None, season: Optional[int] = None
    ) -> dict:
        """
        搜索和匹配TMDB信息
        :param name:  名称
        :param mtype:  类型
        :param year:  年份
        :param season: 用于匹配指定季，0 表示特别季
        """
        # 搜索
        logger.info(f"开始使用 名称：{name} 年份：{year} 匹配TMDB信息 ...")
        info = self.tmdb.match(name=name, year=year, mtype=mtype, season_year=year, season_number=season)
        if info and not info.get("genres"):
            info = self.tmdb.get_info(mtype=info.get("media_type"), tmdbid=info.get("id"))
        return info

    async def async_match_tmdbinfo(
        self, name: str, mtype: MediaType = None, year: Optional[str] = None, season: Optional[int] = None
    ) -> dict:
        """
        异步搜索和匹配TMDB信息
        :param name:  名称
        :param mtype:  类型
        :param year:  年份
        :param season: 用于匹配指定季，0 表示特别季
        """
        # 搜索
        logger.info(f"开始使用 名称：{name} 年份：{year} 匹配TMDB信息 ...")
        info = await self.tmdb.async_match(name=name, year=year, mtype=mtype, season_year=year, season_number=season)
        if info and not info.get("genres"):
            info = await self.tmdb.async_get_info(mtype=info.get("media_type"), tmdbid=info.get("id"))
        return info

    def tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季号；TV 的显式值（含 0）读取季详情，None 或电影的 0 读取媒体详情
        :return: TMDB信息
        """
        if season is None or (season == 0 and mtype != MediaType.TV):
            return self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid)
        else:
            return self.tmdb.get_tv_season_detail(tmdbid=tmdbid, season=season)

    async def async_tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        异步获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季号；TV 的显式值（含 0）读取季详情，None 或电影的 0 读取媒体详情
        :return: TMDB信息
        """
        if season is None or (season == 0 and mtype != MediaType.TV):
            return await self.tmdb.async_get_info(mtype=mtype, tmdbid=tmdbid)
        else:
            return await self.tmdb.async_get_tv_season_detail(tmdbid=tmdbid, season=season)

    def update_recognize_cache(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
    ) -> Optional[bool]:
        """
        回填TMDB本地识别缓存，覆盖名称负缓存，避免共享识别后重复回查。
        """
        if not meta or not mediainfo:
            return None
        if mediainfo.media_source != "themoviedb" or not mediainfo.tmdb_info:
            return None
        self.cache.update(meta, mediainfo.tmdb_info)
        return True

    async def async_update_recognize_cache(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
    ) -> Optional[bool]:
        """
        异步回填TMDB本地识别缓存。
        """
        return self.update_recognize_cache(meta=meta, mediainfo=mediainfo)

    def tmdb_cache_items(self) -> list:
        """
        查询TMDB识别缓存条目列表
        """
        return self.cache.list_items()

    def tmdb_cache_delete(self, cache_key: str) -> dict:
        """
        按缓存键删除单条TMDB识别缓存
        """
        return self.cache.delete(cache_key)

    def tmdb_cache_clear(self) -> None:
        """
        清空全部TMDB识别缓存
        """
        self.cache.clear()

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        plan = self._build_media_search_plan(meta, media_source)
        if not plan:
            return None
        if not plan.name:
            return []
        multi_results = self.tmdb.search_multiis(plan.name) if plan.search_multi else None
        movie_results = self.tmdb.search_movies(plan.name, plan.year) if plan.search_movies else None
        tv_results = self.tmdb.search_tvs(plan.name, plan.year) if plan.search_tvs else None
        results = self._merge_media_search_results(plan, movie_results, tv_results, multi_results)
        # 将搜索词中的季写入标题中
        return self._build_search_medias_result(meta, results)

    def search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[_SchemaMediaPerson]]:
        """
        搜索人物信息
        :param name: 人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        if not is_media_source_enabled(media_source, MediaSource.TMDB):
            return None
        if not name:
            return []
        results = self.tmdb.search_persons(name)
        if results:
            return [_SchemaMediaPerson(source="themoviedb", **person) for person in results]
        return []

    async def async_search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[_SchemaMediaPerson]]:
        """
        异步搜索人物信息
        :param name: 人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        if not is_media_source_enabled(media_source, MediaSource.TMDB):
            return None
        if not name:
            return []
        results = await self.tmdb.async_search_persons(name)
        if results:
            return [_SchemaMediaPerson(source="themoviedb", **person) for person in results]
        return []

    def search_collections(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        :param name: 合集名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        if media_source and not is_media_source_selected(media_source, MediaSource.TMDB):
            return None
        if not name:
            return []
        results = self.tmdb.search_collections(name)
        if results:
            return [MediaInfo(tmdb_info=info) for info in results]
        return []

    async def async_search_collections(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步搜索集合信息
        :param name: 合集名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        if media_source and not is_media_source_selected(media_source, MediaSource.TMDB):
            return None
        if not name:
            return []
        results = await self.tmdb.async_search_collections(name)
        if results:
            return [MediaInfo(tmdb_info=info) for info in results]
        return []

    def tmdb_collection(self, collection_id: int) -> Optional[List[MediaInfo]]:
        """
        根据合集ID查询集合
        :param collection_id:  合集ID
        """
        results = self.tmdb.get_collection(collection_id)
        if results:
            return [MediaInfo(tmdb_info=info) for info in results]
        return []

    def metadata_nfo(
        self, meta: MetaBase, mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None
    ) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or get_runtime_setting("SCRAP_SOURCE")) != "themoviedb":
            return None
        return self.scraper.get_metadata_nfo(meta=meta, mediainfo=mediainfo, season=season, episode=episode)

    def metadata_img(
        self, mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or get_runtime_setting("SCRAP_SOURCE")) != "themoviedb":
            return None
        return self.scraper.get_metadata_img(mediainfo=mediainfo, season=season, episode=episode)

    def tmdb_discover(
        self,
        mtype: MediaType,
        sort_by: str,
        with_genres: str,
        with_original_language: str,
        with_keywords: str,
        with_watch_providers: str,
        vote_average: float,
        vote_count: int,
        release_date: str,
        page: Optional[int] = 1,
    ) -> Optional[List[MediaInfo]]:
        """
        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param with_keywords:  关键字
        :param with_watch_providers:  提供商
        :param vote_average:  评分
        :param vote_count:  评分人数
        :param release_date:  发布日期
        :param page:  页码
        :return: 媒体信息列表
        """
        if mtype == MediaType.MOVIE:
            infos = self.tmdb.discover_movies(
                {
                    "sort_by": sort_by,
                    "with_genres": with_genres,
                    "with_original_language": with_original_language,
                    "with_keywords": with_keywords,
                    "with_watch_providers": with_watch_providers,
                    "vote_average.gte": vote_average,
                    "vote_count.gte": vote_count,
                    "release_date.gte": release_date,
                    "page": page,
                }
            )
        elif mtype == MediaType.TV:
            infos = self.tmdb.discover_tvs(
                {
                    "sort_by": sort_by,
                    "with_genres": with_genres,
                    "with_original_language": with_original_language,
                    "with_keywords": with_keywords,
                    "with_watch_providers": with_watch_providers,
                    "vote_average.gte": vote_average,
                    "vote_count.gte": vote_count,
                    "first_air_date.gte": release_date,
                    "page": page,
                }
            )
        else:
            return []
        if infos:
            return [MediaInfo(tmdb_info=info) for info in infos]
        return []

    def tmdb_trending(self, page: Optional[int] = 1) -> List[MediaInfo]:
        """
        TMDB流行趋势
        :param page: 第几页
        :return: TMDB信息列表
        """
        trending = self.tmdb.discover_trending(page=page)
        if trending:
            return [MediaInfo(tmdb_info=info) for info in trending]
        return []

    def tmdb_seasons(self, tmdbid: int) -> List[_SchemaTmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息
        :param tmdbid:  TMDBID
        """
        tmdb_info = self.tmdb.get_info(tmdbid=tmdbid, mtype=MediaType.TV)
        if not tmdb_info:
            return []
        return [
            _SchemaTmdbSeason(**sea) for sea in tmdb_info.get("seasons", []) if sea.get("season_number") is not None
        ]

    def tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息
        :param group_id: 剧集组ID
        """
        group_seasons = self.tmdb.get_tv_group_seasons(group_id)
        if not group_seasons:
            return []
        return [
            _SchemaTmdbSeason(
                season_number=sea.get("order"),
                name=sea.get("name"),
                episode_count=len(sea.get("episodes") or []),
                air_date=sea.get("episodes")[0].get("air_date") if sea.get("episodes") else None,
            )
            for sea in group_seasons
        ]

    def tmdb_episodes(self, tmdbid: int, season: int, episode_group: Optional[str] = None) -> List[_SchemaTmdbEpisode]:
        """
        根据TMDBID查询某季的所有集信息
        :param tmdbid:  TMDBID
        :param season:  季
        :param episode_group:  剧集组
        """
        if episode_group:
            season_info = self.tmdb.get_tv_group_detail(episode_group, season=season)
        else:
            season_info = self.tmdb.get_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info or not season_info.get("episodes"):
            return []
        return [_SchemaTmdbEpisode(**episode) for episode in season_info.get("episodes")]

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        self.cache.save()

    @staticmethod
    def _validate_obtain_images_params(mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        验证 obtain_images 参数
        :param mediainfo: 媒体信息
        :return: None 表示不处理，MediaInfo 表示继续处理
        """
        if mediainfo.media_source != "themoviedb" and get_runtime_setting("RECOGNIZE_SOURCE") != "themoviedb":
            return None
        if not mediainfo.tmdb_id:
            return mediainfo
        if mediainfo.logo_path and mediainfo.poster_path and mediainfo.backdrop_path:
            # 没有图片缺失
            return mediainfo
        return None

    @staticmethod
    def _build_image_query(mediainfo: MediaInfo) -> _ImageQuery:
        """生成图片查询计划，确保双入口选择同一媒体接口与语言参数。"""
        tmdbid = mediainfo.tmdb_id
        assert tmdbid is not None
        return _ImageQuery(
            tmdbid=tmdbid,
            original_language=mediainfo.original_language,
            movie=mediainfo.type == MediaType.MOVIE,
        )

    @staticmethod
    def _pick_best_tmdb_image(images: list) -> Optional[str]:
        """
        从 TMDB 图片候选中选出评分最高的文件路径。
        """
        if not images:
            return None
        images = sorted(
            images,
            key=lambda x: (
                x.get("vote_average") or 0,
                x.get("vote_count") or 0,
            ),
            reverse=True,
        )
        return images[0].get("file_path")

    @classmethod
    def _process_tmdb_images(cls, mediainfo: MediaInfo, images: dict) -> MediaInfo:
        """
        处理 TMDB 图片数据
        :param mediainfo: 媒体信息
        :param images: 图片数据
        :return: 更新后的媒体信息
        """
        if isinstance(images, list):
            images = images[0]
        # 背景图
        if not mediainfo.backdrop_path:
            if image_path := cls._pick_best_tmdb_image(images.get("backdrops")):
                mediainfo.backdrop_path = get_runtime_setting("TMDB_IMAGE_URL")(image_path)
        # 标志
        if not mediainfo.logo_path:
            if image_path := cls._pick_best_tmdb_image(images.get("logos")):
                mediainfo.logo_path = get_runtime_setting("TMDB_IMAGE_URL")(image_path)
        # 海报
        if not mediainfo.poster_path:
            if image_path := cls._pick_best_tmdb_image(images.get("posters")):
                mediainfo.poster_path = get_runtime_setting("TMDB_IMAGE_URL")(image_path)
        return mediainfo

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        # 验证参数
        result = self._validate_obtain_images_params(mediainfo)
        if result is not None:
            return result

        query = self._build_image_query(mediainfo)
        if query.movie:
            images = self.tmdb.get_movie_images(
                query.tmdbid,
                original_language=query.original_language,
            )
        else:
            images = self.tmdb.get_tv_images(
                query.tmdbid,
                original_language=query.original_language,
            )
        if not images:
            return mediainfo

        # 处理图片数据
        return self._process_tmdb_images(mediainfo, images)

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        # 验证参数
        result = self._validate_obtain_images_params(mediainfo)
        if result is not None:
            return result

        query = self._build_image_query(mediainfo)
        if query.movie:
            images = await self.tmdb.async_get_movie_images(
                query.tmdbid,
                original_language=query.original_language,
            )
        else:
            images = await self.tmdb.async_get_tv_images(
                query.tmdbid,
                original_language=query.original_language,
            )
        if not images:
            return mediainfo

        # 处理图片数据
        return self._process_tmdb_images(mediainfo, images)

    def obtain_specific_image(
        self,
        mediaid: Union[str, int],
        mtype: MediaType,
        image_type: MediaImageType,
        image_prefix: Optional[str] = "w500",
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        获取指定媒体信息图片，返回图片地址
        :param mediaid:     媒体ID
        :param mtype:       媒体类型
        :param image_type:  图片类型
        :param image_prefix: 图片前缀
        :param season:      季
        :param episode:     集
        """
        if not str(mediaid).isdigit():
            return None
        # 图片相对路径
        image_path = None
        image_prefix = image_prefix or "w500"
        if season is None and not episode:
            tmdbinfo = self.tmdb.get_info(mtype=mtype, tmdbid=int(mediaid))
            if tmdbinfo:
                image_path = tmdbinfo.get(image_type.value)
        elif season is not None and episode:
            episodeinfo = self.tmdb.get_tv_episode_detail(tmdbid=int(mediaid), season=season, episode=episode)
            if episodeinfo:
                image_path = episodeinfo.get("still_path")
        elif season is not None:
            seasoninfo = self.tmdb.get_tv_season_detail(tmdbid=int(mediaid), season=season)
            if seasoninfo:
                image_path = seasoninfo.get(image_type.value)

        if image_path:
            return get_runtime_setting("TMDB_IMAGE_URL")(image_path, image_prefix)
        return None

    def tmdb_movie_similar(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询类似电影
        :param tmdbid:  TMDBID
        """
        similar = self.tmdb.get_movie_similar(tmdbid=tmdbid)
        if similar:
            return [MediaInfo(tmdb_info=info) for info in similar]
        return []

    def tmdb_tv_similar(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询类似电视剧
        :param tmdbid:  TMDBID
        """
        similar = self.tmdb.get_tv_similar(tmdbid=tmdbid)
        if similar:
            return [MediaInfo(tmdb_info=info) for info in similar]
        return []

    def tmdb_movie_recommend(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询推荐电影
        :param tmdbid:  TMDBID
        """
        recommend = self.tmdb.get_movie_recommend(tmdbid=tmdbid)
        if recommend:
            return [MediaInfo(tmdb_info=info) for info in recommend]
        return []

    def tmdb_tv_recommend(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询推荐电视剧
        :param tmdbid:  TMDBID
        """
        recommend = self.tmdb.get_tv_recommend(tmdbid=tmdbid)
        if recommend:
            return [MediaInfo(tmdb_info=info) for info in recommend]
        return []

    def tmdb_movie_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = self.tmdb.get_movie_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [_SchemaMediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    def tmdb_tv_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电视剧演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = self.tmdb.get_tv_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [_SchemaMediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    def tmdb_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        根据TMDBID查询人物详情
        :param person_id:  人物ID
        """
        detail = self.tmdb.get_person_detail(person_id=person_id)
        if detail:
            return _SchemaMediaPerson(source="themoviedb", **detail)
        return _SchemaMediaPerson()

    def tmdb_person_credits(self, person_id: int, page: Optional[int] = 1) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品
        :param person_id:  人物ID
        :param page:  页码
        """
        infos = self.tmdb.get_person_credits(person_id=person_id, page=page)
        if infos:
            return [MediaInfo(tmdb_info=tmdbinfo) for tmdbinfo in infos]
        return []

    # 异步方法
    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        plan = self._build_media_search_plan(meta, media_source)
        if not plan:
            return None
        if not plan.name:
            return []
        multi_results = await self.tmdb.async_search_multiis(plan.name) if plan.search_multi else None
        movie_results = await self.tmdb.async_search_movies(plan.name, plan.year) if plan.search_movies else None
        tv_results = await self.tmdb.async_search_tvs(plan.name, plan.year) if plan.search_tvs else None
        results = self._merge_media_search_results(plan, movie_results, tv_results, multi_results)
        # 将搜索词中的季写入标题中
        return self._build_search_medias_result(meta, results)

    async def async_tmdb_discover(
        self,
        mtype: MediaType,
        sort_by: str,
        with_genres: str,
        with_original_language: str,
        with_keywords: str,
        with_watch_providers: str,
        vote_average: float,
        vote_count: int,
        release_date: str,
        page: Optional[int] = 1,
        raise_exception: bool = False,
    ) -> Optional[List[MediaInfo]]:
        """
        TMDB发现功能（异步版本）
        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param with_keywords:  关键字
        :param with_watch_providers:  提供商
        :param vote_average:  评分
        :param vote_count:  评分人数
        :param release_date:  发布日期
        :param page:  页码
        :return: 媒体信息列表
        """
        if mtype == MediaType.MOVIE:
            infos = await self.tmdb.async_discover_movies(
                {
                    "sort_by": sort_by,
                    "with_genres": with_genres,
                    "with_original_language": with_original_language,
                    "with_keywords": with_keywords,
                    "with_watch_providers": with_watch_providers,
                    "vote_average.gte": vote_average,
                    "vote_count.gte": vote_count,
                    "release_date.gte": release_date,
                    "page": page,
                },
                raise_exception=raise_exception,
            )
        elif mtype == MediaType.TV:
            infos = await self.tmdb.async_discover_tvs(
                {
                    "sort_by": sort_by,
                    "with_genres": with_genres,
                    "with_original_language": with_original_language,
                    "with_keywords": with_keywords,
                    "with_watch_providers": with_watch_providers,
                    "vote_average.gte": vote_average,
                    "vote_count.gte": vote_count,
                    "first_air_date.gte": release_date,
                    "page": page,
                },
                raise_exception=raise_exception,
            )
        else:
            return []
        if infos:
            return [MediaInfo(tmdb_info=info) for info in infos]
        return []

    async def async_tmdb_trending(self, page: Optional[int] = 1, raise_exception: bool = False) -> List[MediaInfo]:
        """
        TMDB流行趋势（异步版本）
        :param page: 第几页
        :return: TMDB信息列表
        """
        trending = await self.tmdb.async_discover_trending(
            page=page,
            raise_exception=raise_exception,
        )
        if trending:
            return [MediaInfo(tmdb_info=info) for info in trending]
        return []

    async def async_tmdb_collection(self, collection_id: int) -> Optional[List[MediaInfo]]:
        """
        根据合集ID查询集合（异步版本）
        :param collection_id:  合集ID
        """
        results = await self.tmdb.async_get_collection(collection_id)
        if results:
            return [MediaInfo(tmdb_info=info) for info in results]
        return []

    async def async_tmdb_seasons(self, tmdbid: int) -> List[_SchemaTmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息（异步版本）
        :param tmdbid:  TMDBID
        """
        tmdb_info = await self.tmdb.async_get_info(tmdbid=tmdbid, mtype=MediaType.TV)
        if not tmdb_info:
            return []
        return [
            _SchemaTmdbSeason(**sea) for sea in tmdb_info.get("seasons", []) if sea.get("season_number") is not None
        ]

    async def async_tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息（异步版本）
        :param group_id: 剧集组ID
        """
        group_seasons = await self.tmdb.async_get_tv_group_seasons(group_id)
        if not group_seasons:
            return []
        return [
            _SchemaTmdbSeason(
                season_number=sea.get("order"),
                name=sea.get("name"),
                episode_count=len(sea.get("episodes") or []),
                air_date=sea.get("episodes")[0].get("air_date") if sea.get("episodes") else None,
            )
            for sea in group_seasons
        ]

    async def async_tmdb_episodes(
        self, tmdbid: int, season: int, episode_group: Optional[str] = None
    ) -> List[_SchemaTmdbEpisode]:
        """
        根据TMDBID查询某季的所有集信息（异步版本）
        :param tmdbid:  TMDBID
        :param season:  季
        :param episode_group:  剧集组
        """
        if episode_group:
            season_info = await self.tmdb.async_get_tv_group_detail(episode_group, season=season)
        else:
            season_info = await self.tmdb.async_get_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info or not season_info.get("episodes"):
            return []
        return [_SchemaTmdbEpisode(**episode) for episode in season_info.get("episodes")]

    async def async_tmdb_movie_similar(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询类似电影（异步版本）
        :param tmdbid:  TMDBID
        """
        similar = await self.tmdb.async_get_movie_similar(tmdbid=tmdbid)
        if similar:
            return [MediaInfo(tmdb_info=info) for info in similar]
        return []

    async def async_tmdb_tv_similar(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询类似电视剧（异步版本）
        :param tmdbid:  TMDBID
        """
        similar = await self.tmdb.async_get_tv_similar(tmdbid=tmdbid)
        if similar:
            return [MediaInfo(tmdb_info=info) for info in similar]
        return []

    async def async_tmdb_movie_recommend(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询推荐电影（异步版本）
        :param tmdbid:  TMDBID
        """
        recommend = await self.tmdb.async_get_movie_recommend(tmdbid=tmdbid)
        if recommend:
            return [MediaInfo(tmdb_info=info) for info in recommend]
        return []

    async def async_tmdb_tv_recommend(self, tmdbid: int) -> List[MediaInfo]:
        """
        根据TMDBID查询推荐电视剧（异步版本）
        :param tmdbid:  TMDBID
        """
        recommend = await self.tmdb.async_get_tv_recommend(tmdbid=tmdbid)
        if recommend:
            return [MediaInfo(tmdb_info=info) for info in recommend]
        return []

    async def async_tmdb_movie_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表（异步版本）
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = await self.tmdb.async_get_movie_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [_SchemaMediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    async def async_tmdb_tv_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电视剧演职员表（异步版本）
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = await self.tmdb.async_get_tv_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [_SchemaMediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    async def async_tmdb_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        根据TMDBID查询人物详情（异步版本）
        :param person_id:  人物ID
        """
        detail = await self.tmdb.async_get_person_detail(person_id=person_id)
        if detail:
            return _SchemaMediaPerson(source="themoviedb", **detail)
        return _SchemaMediaPerson()

    async def async_tmdb_person_credits(self, person_id: int, page: Optional[int] = 1) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        :param page:  页码
        """
        infos = await self.tmdb.async_get_person_credits(person_id=person_id, page=page)
        if infos:
            return [MediaInfo(tmdb_info=tmdbinfo) for tmdbinfo in infos]
        return []

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info("开始清除TMDB缓存 ...")
        self.tmdb.clear_cache()
        self.cache.clear()
        logger.info("TMDB缓存清除完成")
