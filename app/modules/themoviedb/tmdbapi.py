"""
TMDB 名称匹配计划、详情消歧及同步异步查询服务。
"""

import re
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Generator, List, Optional, cast

from app.foundation import text as text_tools
from app.foundation.text import convert as zhconv_convert
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType

from .tmdbv3api import TV, Collection, Discover, Episode, Movie, Person, Search, Season, TMDb, Trending
from .tmdbv3api.exceptions import TMDbConnectionError, TMDbException

_MediaRecord = dict[str, Any]
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _MatchTarget(Enum):
    """
    描述一次媒体匹配计划需要调用的真实 TMDB 查询。
    """

    # 按名称与年份查询电影。
    MOVIE_NAME = "movie_name"
    # 按名称与年份查询电视剧。
    TV_NAME = "tv_name"
    # 按名称与目标季年份查询电视剧。
    TV_SEASON = "tv_season"


@dataclass(frozen=True)
class _MatchStep:
    """
    冻结同步与异步入口共同消费的单步匹配计划。

    :param target: 本步骤使用电影名称、电视剧名称或季信息查询
    :param year: 本轮查询年份，None 表示不限制年份
    """

    target: _MatchTarget
    year: Optional[str] = None


@dataclass(frozen=True)
class _CandidatePlan:
    """
    冻结标题直达结果和需要详情补全的候选顺序。

    :param direct: 无需额外详情即可命中的候选，未找到时为 None
    :param aliases: 需要补充详情后检查别名的候选，按尝试顺序保存
    """

    direct: Optional[_MediaRecord]
    aliases: tuple[_MediaRecord, ...]


@dataclass(frozen=True)
class _DetailRequest:
    """
    描述候选决策继续推进前唯一允许执行的详情 I/O。

    :param mtype: 需要查询详情的媒体类型
    :param tmdbid: 候选条目的 TMDB 标识
    """

    mtype: MediaType
    tmdbid: int


class TmdbApi:
    """
    统一 TMDB 名称匹配、媒体详情投影和辅助信息查询。

    同步与异步入口共享识别决策，保持类型消歧、年份回退和缓存语义一致。
    """

    def __init__(self, language: Optional[str] = None):
        """
        按统一语言创建 TMDB 基础及分类接口客户端。

        :param language: 查询语言；未指定时使用运行配置
        """
        self.tmdb = TMDb(language=language)
        self.search = Search(language=language)
        self.movie = Movie(language=language)
        self.tv = TV(language=language)
        self.season_obj = Season(language=language)
        self.episode_obj = Episode(language=language)
        self.discover = Discover(language=language)
        self.trending = Trending(language=language)
        self.person = Person(language=language)
        self.collection = Collection(language=language)

    def search_multiis(self, title: str) -> List[_MediaRecord]:
        """
        同时查询模糊匹配的电影、电视剧TMDB信息。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        multis = self.search.multi(term=title) or []
        return self._project_multi_search_results(multis)

    def search_movies(self, title: str, year: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的电影TMDB信息。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        movies = self.search.movies(**self._search_kwargs(title, year, MediaType.MOVIE)) or []
        return self._project_search_results(title, movies, MediaType.MOVIE)

    def search_tvs(self, title: str, year: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的电视剧TMDB信息。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        tvs = self.search.tv_shows(**self._search_kwargs(title, year, MediaType.TV)) or []
        return self._project_search_results(title, tvs, MediaType.TV)

    def search_persons(self, name: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的人物TMDB信息。

        查询异常由调用方处理；不自动翻页。

        :param name: 待搜索或匹配的名称
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not name:
            return []
        return self.search.people(term=name) or []

    def search_collections(self, name: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的合集TMDB信息。

        查询异常由调用方处理；不自动翻页。

        :param name: 待搜索或匹配的名称
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not name:
            return []
        collections = self.search.collections(term=name) or []
        return self._project_collections(collections)

    def get_collection(self, collection_id: int) -> List[_MediaRecord]:
        """
        根据合集ID查询合集详情。

        :param collection_id: TMDB 电影合集标识
        :return: 合集中的电影条目列表；无效标识或查询失败时为空列表，底层无响应时可能为 None
        """
        if not collection_id:
            return []
        try:
            return self.collection.details(collection_id=collection_id)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
        return []

    @staticmethod
    def __compare_names(file_name: str, tmdb_names: Any) -> bool:
        """
        比较文件名是否匹配，忽略大小写和特殊字符。

        :param file_name: 识别的文件名或者种子名
        :param tmdb_names: TMDB返回的译名
        :return: 标题相符时返回 True，否则返回 False
        """
        if not file_name or not tmdb_names:
            return False
        if not isinstance(tmdb_names, (list, tuple)):
            tmdb_names = [tmdb_names]
        file_name = text_tools.remove_punctuation(file_name).upper()
        for tmdb_name in tmdb_names:
            tmdb_name = text_tools.remove_punctuation(tmdb_name).strip().upper()
            if file_name == tmdb_name:
                return True
        return False

    @staticmethod
    def _search_kwargs(title: str, year: Optional[str], mtype: MediaType) -> dict[str, str]:
        """
        构造同步与异步搜索客户端共享的查询参数。
        """
        kwargs = {"term": title}
        if year:
            kwargs["year" if mtype == MediaType.MOVIE else "release_year"] = year
        return kwargs

    @staticmethod
    def _project_multi_search_results(multis: List[_MediaRecord]) -> List[_MediaRecord]:
        """
        过滤多类型搜索结果并投影为宿主媒体类型。
        """
        ret_infos = []
        for multi in multis:
            media_type = multi.get("media_type")
            if media_type not in ("movie", "tv"):
                continue
            multi["media_type"] = MediaType.MOVIE if media_type == "movie" else MediaType.TV
            ret_infos.append(multi)
        return ret_infos

    @staticmethod
    def _project_search_results(title: str, items: List[_MediaRecord], mtype: MediaType) -> List[_MediaRecord]:
        """
        为来源已完成搜索匹配的候选补齐类型，不按本地化展示标题二次排除。
        """
        del title
        ret_infos = []
        for item in items:
            item["media_type"] = mtype
            ret_infos.append(item)
        return ret_infos

    @staticmethod
    def _project_collections(collections: List[_MediaRecord]) -> List[_MediaRecord]:
        """
        补齐合集查询结果的宿主类型与稳定身份。
        """
        for collection in collections:
            collection["media_type"] = MediaType.COLLECTION
            collection["collection_id"] = collection.get("id")
        return collections

    @staticmethod
    def _validate_match_params(name: str, search_obj: Any) -> bool:
        """
        验证匹配方法的基本参数。
        """
        if not search_obj:
            return False
        if not name:
            return False
        return True

    @staticmethod
    def _generate_year_range(year: Optional[str]) -> List[Optional[str]]:
        """
        生成年份范围用于匹配。
        """
        year_range = [year]
        if year:
            year_range.append(str(int(year) + 1))
            year_range.append(str(int(year) - 1))
        return year_range

    @classmethod
    def _build_match_plan(
        cls,
        mtype: MediaType,
        year: Optional[str],
        season_year: Optional[str],
        season_number: Optional[int],
    ) -> tuple[_MatchStep, ...]:
        """
        冻结名称、年份和季信息对应的有序查询计划。
        """
        if mtype != MediaType.TV:
            return tuple(
                _MatchStep(_MatchTarget.MOVIE_NAME, search_year) for search_year in cls._generate_year_range(year)
            )
        steps = []
        if season_year and season_number is not None:
            steps.append(_MatchStep(_MatchTarget.TV_SEASON, season_year))
        steps.extend(_MatchStep(_MatchTarget.TV_NAME, search_year) for search_year in cls._generate_year_range(year))
        return tuple(steps)

    @classmethod
    def _match_resolution(
        cls, mtype: MediaType, plan: tuple[_MatchStep, ...]
    ) -> Generator[_MatchStep, Optional[_MediaRecord], Optional[_MediaRecord]]:
        """
        统一推进匹配计划并在首个有效结果处短路。
        """
        info: Optional[_MediaRecord] = {}
        for step in plan:
            info = yield step
            if info:
                break
        result_type = MediaType.TV if mtype == MediaType.TV else MediaType.MOVIE
        return cls._set_media_type(info, result_type)

    def _execute_match_step_sync(
        self,
        step: _MatchStep,
        name: str,
        mtype: MediaType,
        season_year: Optional[str],
        season_number: Optional[int],
        group_seasons: Optional[List[_MediaRecord]],
    ) -> Optional[_MediaRecord]:
        """
        通过同步客户端执行一条已冻结的匹配查询。
        """
        if step.target == _MatchTarget.TV_SEASON:
            assert step.year is not None and season_number is not None
            self._log_match_debug(mtype, name, step.year, season_number, season_year)
            return self.__search_tv_by_season(name, step.year, season_number, group_seasons)
        self._log_match_debug(mtype, name, step.year)
        if step.target == _MatchTarget.TV_NAME:
            return self.__search_tv_by_name(name, step.year)
        return self.__search_movie_by_name(name, step.year)

    async def _execute_match_step_async(
        self,
        step: _MatchStep,
        name: str,
        mtype: MediaType,
        season_year: Optional[str],
        season_number: Optional[int],
        group_seasons: Optional[List[_MediaRecord]],
    ) -> Optional[_MediaRecord]:
        """
        通过异步客户端执行一条已冻结的匹配查询。
        """
        if step.target == _MatchTarget.TV_SEASON:
            assert step.year is not None and season_number is not None
            self._log_match_debug(mtype, name, step.year, season_number, season_year)
            return await self.__async_search_tv_by_season(name, step.year, season_number, group_seasons)
        self._log_match_debug(mtype, name, step.year)
        if step.target == _MatchTarget.TV_NAME:
            return await self.__async_search_tv_by_name(name, step.year)
        return await self.__async_search_movie_by_name(name, step.year)

    def _run_match_sync(
        self,
        resolution: Generator[_MatchStep, Optional[_MediaRecord], Optional[_MediaRecord]],
        name: str,
        mtype: MediaType,
        season_year: Optional[str],
        season_number: Optional[int],
        group_seasons: Optional[List[_MediaRecord]],
    ) -> Optional[_MediaRecord]:
        """
        同步执行共享匹配状态机请求的真实 I/O。
        """
        try:
            step = next(resolution)
        except StopIteration as result:
            return cast(Optional[_MediaRecord], result.value)
        while True:
            info = self._execute_match_step_sync(step, name, mtype, season_year, season_number, group_seasons)
            try:
                step = resolution.send(info)
            except StopIteration as result:
                return cast(Optional[_MediaRecord], result.value)

    async def _run_match_async(
        self,
        resolution: Generator[_MatchStep, Optional[_MediaRecord], Optional[_MediaRecord]],
        name: str,
        mtype: MediaType,
        season_year: Optional[str],
        season_number: Optional[int],
        group_seasons: Optional[List[_MediaRecord]],
    ) -> Optional[_MediaRecord]:
        """
        异步执行共享匹配状态机请求的真实 I/O。
        """
        try:
            step = next(resolution)
        except StopIteration as result:
            return cast(Optional[_MediaRecord], result.value)
        while True:
            info = await self._execute_match_step_async(
                step, name, mtype, season_year, season_number, group_seasons
            )
            try:
                step = resolution.send(info)
            except StopIteration as result:
                return cast(Optional[_MediaRecord], result.value)

    @staticmethod
    def _log_match_debug(
        mtype: MediaType,
        name: str,
        year: Optional[str] = None,
        season_number: Optional[int] = None,
        season_year: Optional[str] = None,
    ) -> None:
        """
        记录匹配调试日志。
        """
        if season_number is not None and season_year:
            logger.debug(f"正在识别{mtype.value}：{name}, 季集={season_number}, 季集年份={season_year} ...")
        else:
            logger.debug(f"正在识别{mtype.value}：{name}, 年份={year} ...")

    @staticmethod
    def _set_media_type(info: Optional[_MediaRecord], mtype: MediaType) -> Optional[_MediaRecord]:
        """
        设置媒体类型。
        """
        if info:
            info["media_type"] = mtype
        return info

    @staticmethod
    def _sort_multi_results(multis: List[_MediaRecord]) -> List[_MediaRecord]:
        """
        按年份降序排列搜索结果，电影在前面。
        """
        return sorted(
            multis,
            key=lambda x: (
                ("1" if x.get("media_type") == "movie" else "0")
                + (x.get("release_date") or x.get("first_air_date") or "0000-00-00")
            ),
            reverse=True,
        )

    @staticmethod
    def _convert_media_type(ret_info: _MediaRecord) -> _MediaRecord:
        """
        转换媒体类型为MediaType枚举。
        """
        if ret_info and not isinstance(ret_info.get("media_type"), MediaType):
            ret_info["media_type"] = MediaType.MOVIE if ret_info.get("media_type") == "movie" else MediaType.TV
        return ret_info

    @staticmethod
    def _candidate_media_type(candidate: _MediaRecord) -> Optional[MediaType]:
        """
        把 TMDB 多类型结果值收窄为可查询的宿主媒体类型。
        """
        media_type = candidate.get("media_type")
        if media_type in (MediaType.MOVIE, "movie"):
            return MediaType.MOVIE
        if media_type in (MediaType.TV, "tv"):
            return MediaType.TV
        return None

    @staticmethod
    def _title_fields(mtype: MediaType) -> tuple[str, str]:
        """
        返回媒体类型对应的标题与原标题字段。
        """
        if mtype == MediaType.MOVIE:
            return "title", "original_title"
        return "name", "original_name"

    @classmethod
    def _title_matches(cls, name: str, candidate: _MediaRecord, mtype: MediaType) -> bool:
        """
        使用同一规则比较候选标题和原标题。
        """
        return any(cls.__compare_names(name, candidate.get(field)) for field in cls._title_fields(mtype))

    @classmethod
    def _build_name_candidate_plan(
        cls,
        name: str,
        mtype: MediaType,
        items: List[_MediaRecord],
        year: Optional[str],
    ) -> _CandidatePlan:
        """
        排序、过滤年份并冻结标题优先的候选匹配计划。
        """
        date_field = "release_date" if mtype == MediaType.MOVIE else "first_air_date"
        candidates = sorted(
            items,
            key=lambda item: item.get(date_field) or "0000-00-00",
            reverse=True,
        )
        if year:
            candidates = [item for item in candidates if (item.get(date_field) or "")[:4] == year]
        direct = next(
            (item for item in candidates if cls._title_matches(name, item, mtype)),
            None,
        )
        return _CandidatePlan(direct=direct, aliases=tuple(candidates))

    @classmethod
    def _build_multi_candidate_plan(cls, name: str, multis: List[_MediaRecord]) -> _CandidatePlan:
        """
        冻结多类型结果的排序、标题优先和别名回退顺序。
        """
        candidates = cls._sort_multi_results(multis)
        direct = next(
            (
                item
                for item in candidates
                if (mtype := cls._candidate_media_type(item)) and cls._title_matches(name, item, mtype)
            ),
            None,
        )
        return _CandidatePlan(direct=direct, aliases=tuple(candidates))

    @classmethod
    def _alias_matches(cls, name: str, candidate: Optional[_MediaRecord]) -> bool:
        """
        判断详情候选的别名或译名是否与输入完全一致。
        """
        return bool(candidate and cls.__compare_names(name, candidate.get("names")))

    @classmethod
    def _candidate_resolution(
        cls,
        plan: _CandidatePlan,
        media_type_for: Callable[[_MediaRecord], Optional[MediaType]],
        accepts: Callable[[Optional[_MediaRecord]], bool],
    ) -> Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord]:
        """
        用生成器统一候选短路、详情补全和首个命中决策。
        """
        if plan.direct is not None:
            return plan.direct
        for candidate in plan.aliases:
            resolved: Optional[_MediaRecord] = candidate
            if not candidate.get("names"):
                mtype = media_type_for(candidate)
                tmdbid = candidate.get("id")
                if not mtype or not isinstance(tmdbid, int):
                    continue
                resolved = yield _DetailRequest(mtype=mtype, tmdbid=tmdbid)
            if accepts(resolved):
                return resolved or {}
        return {}

    @classmethod
    def _name_resolution(
        cls, name: str, mtype: MediaType, plan: _CandidatePlan
    ) -> Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord]:
        """
        构造标题搜索候选的别名详情决策。
        """
        return cls._candidate_resolution(
            plan,
            lambda _candidate: mtype,
            lambda candidate: cls._alias_matches(name, candidate),
        )

    @classmethod
    def _multi_resolution(
        cls, name: str, plan: _CandidatePlan
    ) -> Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord]:
        """
        构造多类型候选的类型化详情决策。
        """
        return cls._candidate_resolution(
            plan,
            cls._candidate_media_type,
            lambda candidate: cls._alias_matches(name, candidate),
        )

    def _resolve_details_sync(
        self,
        resolution: Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord],
    ) -> _MediaRecord:
        """
        通过同步客户端执行生成器明确请求的详情 I/O。
        """
        try:
            request = next(resolution)
        except StopIteration as result:
            return cast(_MediaRecord, result.value)
        while True:
            detail = self.get_info(mtype=request.mtype, tmdbid=request.tmdbid)
            try:
                request = resolution.send(detail)
            except StopIteration as result:
                return cast(_MediaRecord, result.value)

    async def _resolve_details_async(
        self,
        resolution: Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord],
    ) -> _MediaRecord:
        """
        通过异步客户端执行生成器明确请求的详情 I/O。
        """
        try:
            request = next(resolution)
        except StopIteration as result:
            return cast(_MediaRecord, result.value)
        while True:
            detail = await self.async_get_info(mtype=request.mtype, tmdbid=request.tmdbid)
            try:
                request = resolution.send(detail)
            except StopIteration as result:
                return cast(_MediaRecord, result.value)

    @staticmethod
    def __get_names(tmdb_info: _MediaRecord) -> List[str]:
        """
        搜索tmdb中所有的标题和译名，用于名称匹配。

        :param tmdb_info: TMDB信息
        :return: 所有译名的清单
        """
        if not tmdb_info:
            return []
        ret_names = []
        if tmdb_info.get("media_type") == MediaType.MOVIE:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("titles", [])
            for alternative_title in alternative_titles:
                title = alternative_title.get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                title = translation.get("data", {}).get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
        else:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                name = alternative_title.get("title")
                if name and name not in ret_names:
                    ret_names.append(name)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                name = translation.get("data", {}).get("name")
                if name and name not in ret_names:
                    ret_names.append(name)
        return ret_names

    def match(
        self,
        name: str,
        mtype: MediaType,
        year: Optional[str] = None,
        season_year: Optional[str] = None,
        season_number: Optional[int] = None,
        group_seasons: Optional[List[_MediaRecord]] = None,
    ) -> Optional[_MediaRecord]:
        """
        搜索tmdb中的媒体信息，匹配返回一条尽可能正确的信息。

        按共享匹配计划依次尝试年份、季信息和别名回退，首个有效候选即返回。

        :param name: 待搜索或匹配的名称
        :param mtype: 电影或电视剧类型；可选入口使用 None 表示未限定
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :param season_year: 目标季的首播年份，可为空
        :param season_number: 目标季编号，0 表示特别篇，None 表示未指定
        :param group_seasons: 已获取的剧集组季集信息，用于季年份匹配
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        """
        if not self._validate_match_params(name, self.search):
            return None
        plan = self._build_match_plan(mtype, year, season_year, season_number)
        return self._run_match_sync(
            self._match_resolution(mtype, plan),
            name,
            mtype,
            season_year,
            season_number,
            group_seasons,
        )

    def __search_movie_by_name(self, name: str, year: Optional[str]) -> Optional[_MediaRecord]:
        """
        根据名称查询电影TMDB匹配。

        :param name: 识别的文件名或种子名
        :param year: 电影上映日期
        :return: 匹配的媒体信息
        """
        try:
            if year:
                movies = self.search.movies(term=name, year=year)
            else:
                movies = self.search.movies(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)} - {traceback.format_exc()}")
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if not movies:
            logger.debug(f"{name} 未找到相关电影信息!")
            return {}
        plan = self._build_name_candidate_plan(name, MediaType.MOVIE, movies, year)
        return self._resolve_details_sync(self._name_resolution(name, MediaType.MOVIE, plan))

    def __search_tv_by_name(self, name: str, year: Optional[str]) -> Optional[_MediaRecord]:
        """
        根据名称查询电视剧TMDB匹配。

        :param name: 识别的文件名或者种子名
        :param year: 电视剧的首播年份
        :return: 匹配的媒体信息
        """
        try:
            if year:
                tvs = self.search.tv_shows(term=name, release_year=year)
            else:
                tvs = self.search.tv_shows(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)} - {traceback.format_exc()}")
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if not tvs:
            logger.debug(f"{name} 未找到相关剧集信息!")
            return {}
        plan = self._build_name_candidate_plan(name, MediaType.TV, tvs, year)
        return self._resolve_details_sync(self._name_resolution(name, MediaType.TV, plan))

    @staticmethod
    def _season_year_matches(
        tv_info: _MediaRecord,
        season_year: str,
        season_number: int,
        group_seasons: Optional[List[_MediaRecord]],
    ) -> bool:
        """
        判断详情中的指定季或集数组是否在目标年份首播。
        """
        if group_seasons:
            for group_season in group_seasons:
                if group_season.get("order") != season_number:
                    continue
                episodes = group_season.get("episodes") or []
                if not episodes or not isinstance(episodes[0], dict):
                    continue
                first_date = episodes[0].get("air_date")
                if isinstance(first_date, str) and _DATE_PATTERN.match(first_date):
                    return first_date[:4] == str(season_year)
            return False
        for season_info in tv_info.get("seasons") or []:
            if season_info.get("season_number") != season_number:
                continue
            air_date = season_info.get("air_date") or ""
            if air_date[:4] == str(season_year):
                return True
        return False

    @classmethod
    def _season_candidate_matches(
        cls,
        name: str,
        candidate: Optional[_MediaRecord],
        season_year: str,
        season_number: int,
        group_seasons: Optional[List[_MediaRecord]],
    ) -> bool:
        """
        按标题、别名、首播年份和季年份判断剧集候选。
        """
        if not candidate:
            return False
        if not (cls._title_matches(name, candidate, MediaType.TV) or cls._alias_matches(name, candidate)):
            return False
        first_air_year = (candidate.get("first_air_date") or "")[:4]
        return first_air_year == str(season_year) or cls._season_year_matches(
            candidate, season_year, season_number, group_seasons
        )

    @classmethod
    def _build_season_candidate_plan(
        cls,
        name: str,
        tvs: List[_MediaRecord],
        season_year: str,
    ) -> _CandidatePlan:
        """
        冻结电视剧首播年份直达和详情回退候选顺序。
        """
        candidates = sorted(
            tvs,
            key=lambda item: item.get("first_air_date") or "0000-00-00",
            reverse=True,
        )
        direct = next(
            (
                item
                for item in candidates
                if cls._title_matches(name, item, MediaType.TV)
                and (item.get("first_air_date") or "")[:4] == str(season_year)
            ),
            None,
        )
        return _CandidatePlan(direct=direct, aliases=tuple(candidates))

    @classmethod
    def _season_resolution(
        cls,
        name: str,
        plan: _CandidatePlan,
        season_year: str,
        season_number: int,
        group_seasons: Optional[List[_MediaRecord]],
    ) -> Generator[_DetailRequest, Optional[_MediaRecord], _MediaRecord]:
        """
        构造剧集候选的标题、别名和季年份详情决策。
        """
        return cls._candidate_resolution(
            plan,
            lambda _candidate: MediaType.TV,
            lambda candidate: cls._season_candidate_matches(name, candidate, season_year, season_number, group_seasons),
        )

    def __search_tv_by_season(
        self,
        name: str,
        season_year: str,
        season_number: int,
        group_seasons: Optional[List[_MediaRecord]] = None,
    ) -> Optional[_MediaRecord]:
        """
        根据电视剧的名称和季的年份及序号匹配TMDB。

        :param name: 识别的文件名或者种子名
        :param season_year: 季的年份
        :param season_number: 季序号
        :param group_seasons: 集数组信息
        :return: 匹配的媒体信息
        """
        try:
            tvs = self.search.tv_shows(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
            print(traceback.format_exc())
            return None

        if not tvs:
            logger.debug(f"{name} 未找到季{season_number}相关信息!")
            return {}
        plan = self._build_season_candidate_plan(name, tvs, season_year)
        return self._resolve_details_sync(
            self._season_resolution(name, plan, season_year, season_number, group_seasons)
        )

    def match_multi(self, name: str) -> Optional[_MediaRecord]:
        """
        根据名称同时查询电影和电视剧，没有类型也没有年份时使用。

        按共享匹配计划依次尝试年份、季信息和别名回退，首个有效候选即返回。

        :param name: 待搜索或匹配的名称
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        """
        try:
            multis = self.search.multi(term=name) or []
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
            print(traceback.format_exc())
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")

        if not multis:
            logger.debug(f"{name} 未找到相关媒体息!")
            return {}
        plan = self._build_multi_candidate_plan(name, multis)
        resolved = self._resolve_details_sync(self._multi_resolution(name, plan))
        return self._convert_media_type(resolved)

    @staticmethod
    def _info_lookup_types(mtype: Optional[MediaType]) -> tuple[MediaType, ...]:
        """
        返回详情查询的稳定类型顺序，未知类型固定先剧集后电影。
        """
        if mtype == MediaType.MOVIE:
            return (MediaType.MOVIE,)
        if mtype == MediaType.TV:
            return (MediaType.TV,)
        return MediaType.TV, MediaType.MOVIE

    @staticmethod
    def _select_info(
        mtype: Optional[MediaType],
        tmdbid: int,
        details: dict[MediaType, Optional[_MediaRecord]],
    ) -> Optional[_MediaRecord]:
        """
        从查询结果中统一判断类型、歧义和未命中状态。
        """
        if mtype in (MediaType.MOVIE, MediaType.TV):
            tmdb_info = details.get(mtype)
            if tmdb_info:
                tmdb_info["media_type"] = mtype
            return tmdb_info
        tv_info = details.get(MediaType.TV)
        movie_info = details.get(MediaType.MOVIE)
        if tv_info and movie_info:
            logger.warning(f"无法判断tmdb_id:{tmdbid} 是电影还是电视剧")
            return None
        if tv_info:
            tv_info["media_type"] = MediaType.TV
            return tv_info
        if movie_info:
            movie_info["media_type"] = MediaType.MOVIE
            return movie_info
        logger.warning(f"tmdb_id:{tmdbid} 未查询到媒体信息")
        return None

    @classmethod
    def _info_resolution(
        cls, mtype: Optional[MediaType], tmdbid: int
    ) -> Generator[MediaType, Optional[_MediaRecord], Optional[_MediaRecord]]:
        """
        统一推进详情类型查询顺序并完成歧义判断。
        """
        details: dict[MediaType, Optional[_MediaRecord]] = {}
        for detail_type in cls._info_lookup_types(mtype):
            details[detail_type] = yield detail_type
        return cls._select_info(mtype, tmdbid, details)

    def _run_info_sync(
        self,
        resolution: Generator[MediaType, Optional[_MediaRecord], Optional[_MediaRecord]],
        tmdbid: int,
        raise_on_connection_error: bool,
    ) -> Optional[_MediaRecord]:
        """
        同步执行共享详情状态机请求的真实 I/O。
        """
        try:
            detail_type = next(resolution)
        except StopIteration as result:
            return cast(Optional[_MediaRecord], result.value)
        while True:
            if detail_type == MediaType.TV:
                detail = self.__get_tv_detail(
                    tmdbid,
                    raise_on_connection_error=raise_on_connection_error,
                )
            else:
                detail = self.__get_movie_detail(
                    tmdbid,
                    raise_on_connection_error=raise_on_connection_error,
                )
            try:
                detail_type = resolution.send(detail)
            except StopIteration as result:
                return cast(Optional[_MediaRecord], result.value)

    async def _run_info_async(
        self,
        resolution: Generator[MediaType, Optional[_MediaRecord], Optional[_MediaRecord]],
        tmdbid: int,
        raise_on_connection_error: bool,
    ) -> Optional[_MediaRecord]:
        """
        异步执行共享详情状态机请求的真实 I/O。
        """
        try:
            detail_type = next(resolution)
        except StopIteration as result:
            return cast(Optional[_MediaRecord], result.value)
        while True:
            if detail_type == MediaType.TV:
                detail = await self.__async_get_tv_detail(
                    tmdbid,
                    raise_on_connection_error=raise_on_connection_error,
                )
            else:
                detail = await self.__async_get_movie_detail(
                    tmdbid,
                    raise_on_connection_error=raise_on_connection_error,
                )
            try:
                detail_type = resolution.send(detail)
            except StopIteration as result:
                return cast(Optional[_MediaRecord], result.value)

    def _project_info(self, tmdb_info: Optional[_MediaRecord]) -> Optional[_MediaRecord]:
        """
        把 TMDB 详情统一投影为宿主识别所需字段。
        """
        if not tmdb_info:
            return tmdb_info
        tmdb_info["genre_ids"] = [genre.get("id") for genre in tmdb_info.get("genres") or []]
        tmdb_info["names"] = self.__get_names(tmdb_info)
        tmdb_info["content_rating"] = self.__get_content_rating(tmdb_info)
        self.__update_tmdbinfo_extra_title(tmdb_info)
        if self.tmdb.language in ("zh", "zh-CN"):
            self.__update_tmdbinfo_cn_title(tmdb_info)
        return tmdb_info

    def get_info(
        self, mtype: Optional[MediaType], tmdbid: int, raise_on_connection_error: bool = False
    ) -> Optional[_MediaRecord]:
        """
        按 TMDB 标识查询详情，并补充规范类型、别名和分级。

        类型未知时同时考虑电影和电视剧；详情身份仍存在歧义时不任意选取。

        :param mtype: 电影或电视剧类型；可选入口使用 None 表示未限定
        :param tmdbid: TMDB 数字媒体标识
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        tmdb_info = self._run_info_sync(
            self._info_resolution(mtype, tmdbid),
            tmdbid,
            raise_on_connection_error,
        )
        return self._project_info(tmdb_info)

    @staticmethod
    def __get_content_rating(tmdb_info: _MediaRecord) -> Optional[str]:
        """
        获得tmdb中的内容评级。

        :param tmdb_info: TMDB信息
        :return: 内容评级
        """
        if not tmdb_info:
            return None
        ratings = {}
        if results := (tmdb_info.get("release_dates") or {}).get("results"):
            """
            [
                {
                    "iso_3166_1": "AR",
                    "release_dates": [
                        {
                            "certification": "+13",
                            "descriptors": [],
                            "iso_639_1": "",
                            "note": "",
                            "release_date": "2025-01-23T00:00:00.000Z",
                            "type": 3
                        }
                    ]
                }
            ]
            """
            for item in results:
                iso_3166_1 = item.get("iso_3166_1")
                if not iso_3166_1:
                    continue
                dates = item.get("release_dates")
                if not dates:
                    continue
                certification = dates[0].get("certification")
                if not certification:
                    continue
                ratings[iso_3166_1] = certification
        elif results := (tmdb_info.get("content_ratings") or {}).get("results"):
            """
            [
                {
                    "descriptors": [],
                    "iso_3166_1": "US",
                    "rating": "TV-MA"
                }
            ]
            """
            for item in results:
                iso_3166_1 = item.get("iso_3166_1")
                if not iso_3166_1:
                    continue
                rating = item.get("rating")
                if not rating:
                    continue
                ratings[iso_3166_1] = rating
        if not ratings:
            return None
        return ratings.get("CN") or ratings.get("US")

    @staticmethod
    def __update_tmdbinfo_cn_title(tmdb_info: _MediaRecord) -> None:
        """
        更新TMDB信息中的中文名称。
        """

        def __get_tmdb_chinese_title(tmdbinfo: _MediaRecord) -> Optional[str]:
            """
            从别名中获取中文标题。
            """
            if not tmdbinfo:
                return None
            if tmdbinfo.get("media_type") == MediaType.MOVIE:
                alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
            else:
                alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                iso_3166_1 = alternative_title.get("iso_3166_1")
                if iso_3166_1 == "CN":
                    title = alternative_title.get("title")
                    if isinstance(title, str) and text_tools.contains_chinese(title) \
                            and zhconv_convert(title, "zh-hans") == title:
                        return title
            title = tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")
            return title if isinstance(title, str) else None

        org_title = tmdb_info.get("title") \
            if tmdb_info.get("media_type") == MediaType.MOVIE \
            else tmdb_info.get("name")
        if not text_tools.contains_chinese(org_title if isinstance(org_title, str) else ""):
            cn_title = __get_tmdb_chinese_title(tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title
            else:
                sg_title = tmdb_info.get("sg_title")
                if sg_title and sg_title != org_title and text_tools.contains_chinese(sg_title):
                    if tmdb_info.get("media_type") == MediaType.MOVIE:
                        tmdb_info['title'] = sg_title
                    else:
                        tmdb_info['name'] = sg_title

    @staticmethod
    def __update_tmdbinfo_extra_title(tmdb_info: _MediaRecord) -> None:
        """
        更新TMDB信息中的其它语种名称。
        """

        def __get_tmdb_lang_title(tmdbinfo: _MediaRecord, lang: Optional[str] = "US") -> Optional[str]:
            """
            从译名中获取其它语种标题。
            """
            if not tmdbinfo:
                return None
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                if translation.get("iso_3166_1") == lang:
                    title = translation.get("data", {}).get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE \
                        else translation.get("data", {}).get("name")
                    return title if isinstance(title, str) else None
            return None

        org_title = (
            tmdb_info.get("original_title")
            if tmdb_info.get("media_type") == MediaType.MOVIE
            else tmdb_info.get("original_name")
        )
        if tmdb_info.get("original_language") == "en":
            tmdb_info['en_title'] = org_title
        else:
            en_title = __get_tmdb_lang_title(tmdb_info, "US")
            tmdb_info['en_title'] = en_title or org_title

        tmdb_info['hk_title'] = __get_tmdb_lang_title(tmdb_info, "HK")
        tmdb_info['tw_title'] = __get_tmdb_lang_title(tmdb_info, "TW")

        # 查找新加坡名（用于替代中文名）
        tmdb_info['sg_title'] = __get_tmdb_lang_title(tmdb_info, "SG") or org_title

    def __get_movie_detail(self,
                           tmdbid: int,
                           append_to_response: Optional[str] = "images,"
                                                               "credits,"
                                                               "alternative_titles,"
                                                               "translations,"
                                                               "release_dates,"
                                                               "external_ids",
                           raise_on_connection_error: bool = False) -> Optional[_MediaRecord]:
        """
        查询电影或电视剧详情，保留连接失败与空结果的区别。

        :param tmdbid: TMDB 数字媒体标识
        :param append_to_response: 逗号分隔的附加详情接口名称
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: TMDB 详情；客户端不可用或无数据时为空字典，查询异常默认返回 None
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        if not self.movie:
            return {}
        try:
            logger.debug("正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            if tmdbinfo:
                logger.debug(f"{tmdbid} 查询结果：{tmdbinfo.get('title')}")
            return tmdbinfo or {}
        except TMDbConnectionError as err:
            logger.error(str(err))
            if raise_on_connection_error:
                raise
            return None
        except Exception as e:
            logger.error(str(e))
            return None

    def __get_tv_detail(self,
                        tmdbid: int,
                        append_to_response: Optional[str] = "images,"
                                                            "credits,"
                                                            "alternative_titles,"
                                                            "translations,"
                                                            "content_ratings,"
                                                            "external_ids,"
                                                            "episode_groups",
                        raise_on_connection_error: bool = False) -> Optional[_MediaRecord]:
        """
        查询电影或电视剧详情，保留连接失败与空结果的区别。

        :param tmdbid: TMDB 数字媒体标识
        :param append_to_response: 逗号分隔的附加详情接口名称
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: TMDB 详情；客户端不可用或无数据时为空字典，查询异常默认返回 None
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        if not self.tv:
            return {}
        try:
            logger.debug("正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tv_id=tmdbid, append_to_response=append_to_response)
            if tmdbinfo:
                logger.debug(f"{tmdbid} 查询结果：{tmdbinfo.get('name')}")
            return tmdbinfo or {}
        except TMDbConnectionError as err:
            logger.error(str(err))
            if raise_on_connection_error:
                raise
            return None
        except Exception as e:
            logger.error(str(e))
            return None

    def get_tv_season_detail(self, tmdbid: int, season: int) -> _MediaRecord:
        """
        获取电视剧季的详情。

        :param tmdbid: TMDB 数字媒体标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        if not self.season_obj:
            return {}
        try:
            logger.debug("正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = self.season_obj.details(tv_id=tmdbid, season_num=season)
            return tmdbinfo or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    def get_tv_episode_detail(self, tmdbid: int, season: int, episode: int) -> _MediaRecord:
        """
        获取电视剧集的详情。

        :param tmdbid: TMDB 数字媒体标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :param episode: 目标集号，可选入口非零时表示单集
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        if not self.episode_obj:
            return {}
        try:
            logger.debug("正在查询TMDB集详情：%s，季：%s，集：%s ..." % (tmdbid, season, episode))
            tmdbinfo = self.episode_obj.details(tv_id=tmdbid, season_num=season, episode_num=episode)
            return tmdbinfo or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    def discover_movies(self, params: _MediaRecord) -> List[_MediaRecord]:
        """
        发现电影。

        :param params: 按 TMDB 发现接口命名的查询参数字典
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.discover:
            return []
        try:
            logger.debug(f"正在发现电影：{params}...")
            tmdbinfo = self.discover.discover_movies(tuple(params.items()))
            if tmdbinfo:
                for info in tmdbinfo:
                    info['media_type'] = MediaType.MOVIE
            return tmdbinfo or []
        except Exception as e:
            logger.error(str(e))
            return []

    def discover_tvs(self, params: _MediaRecord) -> List[_MediaRecord]:
        """
        发现电视剧。

        :param params: 按 TMDB 发现接口命名的查询参数字典
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.discover:
            return []
        try:
            logger.debug(f"正在发现电视剧：{params}...")
            tmdbinfo = self.discover.discover_tv_shows(tuple(params.items()))
            if tmdbinfo:
                for info in tmdbinfo:
                    info['media_type'] = MediaType.TV
            return tmdbinfo or []
        except Exception as e:
            logger.error(str(e))
            return []

    @staticmethod
    def _normalize_trending_infos(infos: Optional[List[_MediaRecord]]) -> List[_MediaRecord]:
        """
        过滤流行趋势中的人物等非媒体项，并统一电影、电视剧的媒体类型。
        """
        if not infos:
            return []

        ret_infos = []
        for info in infos:
            media_type = info.get("media_type")
            if media_type == "movie":
                info["media_type"] = MediaType.MOVIE
            elif media_type == "tv":
                info["media_type"] = MediaType.TV
            elif media_type not in [MediaType.MOVIE, MediaType.TV]:
                continue
            ret_infos.append(info)
        return ret_infos

    def discover_trending(self, page: Optional[int] = 1) -> List[_MediaRecord]:
        """
        查询周趋势榜中的电影和电视剧。

        :param page: 请求页码，从 1 开始
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.trending:
            return []
        try:
            logger.debug(f"正在获取流行趋势：page={page} ...")
            tmdbinfo = self.trending.all_week(page=page)
            return self._normalize_trending_infos(tmdbinfo)
        except Exception as e:
            logger.error(str(e))
            return []

    @staticmethod
    def _build_include_image_language(original_language: Optional[str] = None) -> str:
        """
        构造图片接口语言回退列表，避免当前语言没有图片时返回空列表。
        """
        languages = []
        for language in (
                get_runtime_setting('TMDB_LOCALE'),
                "en",
                None,
                original_language,
        ):
            language = "null" if language is None else str(language).strip()
            if language and language not in languages:
                languages.append(language)
        return ",".join(languages)

    def get_movie_images(self, tmdbid: int, original_language: Optional[str] = None) -> _MediaRecord:
        """
        获取电影的图片。

        :param tmdbid: TMDB 数字媒体标识
        :param original_language: 媒体原始语言，加入图片语言回退列表
        :return: 图片分类字典；无结果或查询失败时为空字典
        """
        if not self.movie:
            return {}
        try:
            logger.debug(f"正在获取电影图片：{tmdbid}...")
            return self.movie.images(
                movie_id=tmdbid,
                include_image_language=self._build_include_image_language(
                    original_language
                ),
            ) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    def get_tv_images(self, tmdbid: int, original_language: Optional[str] = None) -> _MediaRecord:
        """
        获取电视剧的图片。

        :param tmdbid: TMDB 数字媒体标识
        :param original_language: 媒体原始语言，加入图片语言回退列表
        :return: 图片分类字典；无结果或查询失败时为空字典
        """
        if not self.tv:
            return {}
        try:
            logger.debug(f"正在获取电视剧图片：{tmdbid}...")
            return self.tv.images(
                tv_id=tmdbid,
                include_image_language=self._build_include_image_language(
                    original_language
                ),
            ) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    def get_movie_similar(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电影的相似电影。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取相似电影：{tmdbid}...")
            return self.movie.similar(movie_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_tv_similar(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电视剧的相似电视剧。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取相似电视剧：{tmdbid}...")
            return self.tv.similar(tv_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_movie_recommend(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电影的推荐电影。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取推荐电影：{tmdbid}...")
            return self.movie.recommendations(movie_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_tv_recommend(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电视剧的推荐电视剧。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取推荐电视剧：{tmdbid}...")
            return self.tv.recommendations(tv_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_movie_credits(self, tmdbid: int, page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取电影的演员列表。

        仅取 cast 演员字段，不包含 crew 工作人员；拉取完整演员表后在本地分页。

        :param tmdbid: TMDB 数字媒体标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取电影演职人员：{tmdbid}...")
            info = self.movie.credits(movie_id=tmdbid) or {}
            cast = info.get('cast') or []
            if cast:
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_tv_credits(self, tmdbid: int, page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取电视剧的演员列表。

        仅取 cast 演员字段，不包含 crew 工作人员；拉取完整演员表后在本地分页。

        :param tmdbid: TMDB 数字媒体标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取电视剧演职人员：{tmdbid}...")
            info = self.tv.credits(tv_id=tmdbid) or {}
            cast = info.get('cast') or []
            if cast:
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    def get_tv_group_seasons(self, group_id: str) -> List[_MediaRecord]:
        """
        获取电视剧剧集组季集列表。

        保留剧集组顺序，并将每组 episode_number 重新编号为从 1 开始。

        :param group_id: TMDB 剧集组标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取剧集组：{group_id}...")
            group_seasons = self.tv.group_episodes(group_id) or []
            return [
                {
                    **group_season,
                    "episodes": [
                        {**ep, "episode_number": idx}
                        # 剧集组中每个季的episode_number从1开始
                        for idx, ep in enumerate(group_season.get("episodes", []), start=1)
                    ]
                }
                for group_season in group_seasons
            ]
        except Exception as e:
            logger.error(str(e))
            return []

    def get_tv_group_detail(self, group_id: str, season: int) -> _MediaRecord:
        """
        获取剧集组某个季的信息。

        :param group_id: TMDB 剧集组标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        group_seasons = self.get_tv_group_seasons(group_id)
        if not group_seasons:
            return {}
        for group_season in group_seasons:
            if group_season.get('order') == season:
                return group_season
        return {}

    def get_person_detail(self, person_id: int) -> _MediaRecord:
        """
        查询人物详情。

        :param person_id: TMDB 人物标识
        :return: 人物详情字典；无结果或查询失败时为空字典
        """
        if not self.person:
            return {}
        try:
            logger.debug(f"正在获取人物详情：{person_id}...")
            return self.person.details(person_id=person_id) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    def get_person_credits(self, person_id: int, page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取人物参演作品。

        合并电影与电视剧的 cast 作品，按上映或首播日期倒序后在本地分页。

        :param person_id: TMDB 人物标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.person:
            return []
        try:
            logger.debug(f"正在获取人物参演作品：{person_id}...")
            movies = self.person.movie_credits(person_id=person_id) or {}
            tvs = self.person.tv_credits(person_id=person_id) or {}
            cast = (movies.get('cast') or []) + (tvs.get('cast') or [])
            if cast:
                cast = sorted(cast, key=lambda x: x.get('release_date') or x.get('first_air_date') or '1900-01-01',
                              reverse=True)
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    def clear_cache(self) -> None:
        """
        清理发现结果缓存和同步 HTTP 响应缓存。
        """
        getattr(self.discover.discover_movies, "cache_clear", lambda: None)()
        getattr(self.discover.discover_tv_shows, "cache_clear", lambda: None)()
        self.tmdb.cache_clear()

    async def __async_search_movie_by_name(self, name: str, year: Optional[str]) -> Optional[_MediaRecord]:
        """
        根据名称查询电影TMDB匹配（异步版本）。

        :param name: 识别的文件名或种子名
        :param year: 电影上映日期
        :return: 匹配的媒体信息
        """
        try:
            if year:
                movies = await self.search.async_movies(term=name, year=year)
            else:
                movies = await self.search.async_movies(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)} - {traceback.format_exc()}")
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if not movies:
            logger.debug(f"{name} 未找到相关电影信息!")
            return {}
        plan = self._build_name_candidate_plan(name, MediaType.MOVIE, movies, year)
        return await self._resolve_details_async(self._name_resolution(name, MediaType.MOVIE, plan))

    async def __async_search_tv_by_name(self, name: str, year: Optional[str]) -> Optional[_MediaRecord]:
        """
        根据名称查询电视剧TMDB匹配（异步版本）。

        :param name: 识别的文件名或者种子名
        :param year: 电视剧的首播年份
        :return: 匹配的媒体信息
        """
        try:
            if year:
                tvs = await self.search.async_tv_shows(term=name, release_year=year)
            else:
                tvs = await self.search.async_tv_shows(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)} - {traceback.format_exc()}")
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if not tvs:
            logger.debug(f"{name} 未找到相关剧集信息!")
            return {}
        plan = self._build_name_candidate_plan(name, MediaType.TV, tvs, year)
        return await self._resolve_details_async(self._name_resolution(name, MediaType.TV, plan))

    async def __async_search_tv_by_season(
        self,
        name: str,
        season_year: str,
        season_number: int,
        group_seasons: Optional[List[_MediaRecord]] = None,
    ) -> Optional[_MediaRecord]:
        """
        根据电视剧的名称和季的年份及序号匹配TMDB（异步版本）。

        :param name: 识别的文件名或者种子名
        :param season_year: 季的年份
        :param season_number: 季序号
        :param group_seasons: 集数组信息
        :return: 匹配的媒体信息
        """
        try:
            tvs = await self.search.async_tv_shows(term=name)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
            print(traceback.format_exc())
            return None

        if not tvs:
            logger.debug(f"{name} 未找到季{season_number}相关信息!")
            return {}
        plan = self._build_season_candidate_plan(name, tvs, season_year)
        return await self._resolve_details_async(
            self._season_resolution(name, plan, season_year, season_number, group_seasons)
        )

    async def __async_get_movie_detail(self,
                                       tmdbid: int,
                                       append_to_response: Optional[str] = "images,"
                                                                           "credits,"
                                                                           "alternative_titles,"
                                                                           "translations,"
                                                                           "release_dates,"
                                                                           "external_ids",
                                       raise_on_connection_error: bool = False) -> Optional[_MediaRecord]:
        """
        查询电影或电视剧详情，保留连接失败与空结果的区别。

        :param tmdbid: TMDB 数字媒体标识
        :param append_to_response: 逗号分隔的附加详情接口名称
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: TMDB 详情；客户端不可用或无数据时为空字典，查询异常默认返回 None
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        if not self.movie:
            return {}
        try:
            logger.debug("正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = await self.movie.async_details(tmdbid, append_to_response)
            if tmdbinfo:
                logger.debug(f"{tmdbid} 查询结果：{tmdbinfo.get('title')}")
            return tmdbinfo or {}
        except TMDbConnectionError as err:
            logger.error(str(err))
            if raise_on_connection_error:
                raise
            return None
        except Exception as e:
            logger.error(str(e))
            return None

    async def __async_get_tv_detail(self,
                                    tmdbid: int,
                                    append_to_response: Optional[str] = "images,"
                                                                        "credits,"
                                                                        "alternative_titles,"
                                                                        "translations,"
                                                                        "content_ratings,"
                                                                        "external_ids,"
                                                                        "episode_groups",
                                    raise_on_connection_error: bool = False) -> Optional[_MediaRecord]:
        """
        查询电影或电视剧详情，保留连接失败与空结果的区别。

        :param tmdbid: TMDB 数字媒体标识
        :param append_to_response: 逗号分隔的附加详情接口名称
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: TMDB 详情；客户端不可用或无数据时为空字典，查询异常默认返回 None
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        if not self.tv:
            return {}
        try:
            logger.debug("正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = await self.tv.async_details(tv_id=tmdbid, append_to_response=append_to_response)
            if tmdbinfo:
                logger.debug(f"{tmdbid} 查询结果：{tmdbinfo.get('name')}")
            return tmdbinfo or {}
        except TMDbConnectionError as err:
            logger.error(str(err))
            if raise_on_connection_error:
                raise
            return None
        except Exception as e:
            logger.error(str(e))
            return None

    async def async_search_multiis(self, title: str) -> List[_MediaRecord]:
        """
        同时查询模糊匹配的电影、电视剧TMDB信息（异步版本）。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        multis = await self.search.async_multi(term=title) or []
        return self._project_multi_search_results(multis)

    async def async_search_movies(self, title: str, year: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的电影TMDB信息（异步版本）。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        movies = await self.search.async_movies(**self._search_kwargs(title, year, MediaType.MOVIE)) or []
        return self._project_search_results(title, movies, MediaType.MOVIE)

    async def async_search_tvs(self, title: str, year: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的电视剧TMDB信息（异步版本）。

        查询异常由调用方处理；不自动翻页。

        :param title: 待搜索标题，空值不发起查询
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not title:
            return []
        tvs = await self.search.async_tv_shows(**self._search_kwargs(title, year, MediaType.TV)) or []
        return self._project_search_results(title, tvs, MediaType.TV)

    async def async_discover_movies(
            self, params: _MediaRecord, raise_exception: bool = False
    ) -> List[_MediaRecord]:
        """
        发现电影（异步版本）。

        :param params: 按 TMDB 发现接口命名的查询参数字典
        :param raise_exception: True 时重新抛出查询异常，False 时记录日志并返回空列表
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        :raises Exception: 开启异常传播且底层查询失败
        """
        if not params:
            return []
        try:
            items = await self.discover.async_discover_movies(params_tuple=tuple(params.items())) or []
            for item in items:
                item['media_type'] = MediaType.MOVIE
            return items
        except Exception as e:
            logger.error(f"获取电影发现失败：{str(e)}")
            if raise_exception:
                raise
            return []

    async def async_discover_tvs(
            self, params: _MediaRecord, raise_exception: bool = False
    ) -> List[_MediaRecord]:
        """
        发现电视剧（异步版本）。

        :param params: 按 TMDB 发现接口命名的查询参数字典
        :param raise_exception: True 时重新抛出查询异常，False 时记录日志并返回空列表
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        :raises Exception: 开启异常传播且底层查询失败
        """
        if not params:
            return []
        try:
            items = await self.discover.async_discover_tv_shows(params_tuple=tuple(params.items())) or []
            for item in items:
                item['media_type'] = MediaType.TV
            return items
        except Exception as e:
            logger.error(f"获取电视剧发现失败：{str(e)}")
            if raise_exception:
                raise
            return []

    async def async_search_persons(self, name: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的人物TMDB信息（异步版本）。

        查询异常由调用方处理；不自动翻页。

        :param name: 待搜索或匹配的名称
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not name:
            return []
        return await self.search.async_people(term=name) or []

    async def async_search_collections(self, name: str) -> List[_MediaRecord]:
        """
        查询模糊匹配的当前页的合集TMDB信息（异步版本）。

        查询异常由调用方处理；不自动翻页。

        :param name: 待搜索或匹配的名称
        :return: 当前页的结果字典列表；关键词为空或无匹配时为空列表
        """
        if not name:
            return []
        collections = await self.search.async_collections(term=name) or []
        return self._project_collections(collections)

    async def async_get_collection(self, collection_id: int) -> List[_MediaRecord]:
        """
        根据合集ID查询合集详情（异步版本）。

        :param collection_id: TMDB 电影合集标识
        :return: 合集中的电影条目列表；无效标识或查询失败时为空列表，底层无响应时可能为 None
        """
        if not collection_id:
            return []
        try:
            return await self.collection.async_details(collection_id=collection_id)
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
        return []

    async def async_match(
        self,
        name: str,
        mtype: MediaType,
        year: Optional[str] = None,
        season_year: Optional[str] = None,
        season_number: Optional[int] = None,
        group_seasons: Optional[List[_MediaRecord]] = None,
    ) -> Optional[_MediaRecord]:
        """
        搜索tmdb中的媒体信息，匹配返回一条尽可能正确的信息（异步版本）。

        按共享匹配计划依次尝试年份、季信息和别名回退，首个有效候选即返回。

        :param name: 待搜索或匹配的名称
        :param mtype: 电影或电视剧类型；可选入口使用 None 表示未限定
        :param year: 作品上映或整剧首播年份，未指定时不按年份限制
        :param season_year: 目标季的首播年份，可为空
        :param season_number: 目标季编号，0 表示特别篇，None 表示未指定
        :param group_seasons: 已获取的剧集组季集信息，用于季年份匹配
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        """
        if not self._validate_match_params(name, self.search):
            return None
        plan = self._build_match_plan(mtype, year, season_year, season_number)
        return await self._run_match_async(
            self._match_resolution(mtype, plan),
            name,
            mtype,
            season_year,
            season_number,
            group_seasons,
        )

    async def async_match_multi(self, name: str) -> Optional[_MediaRecord]:
        """
        根据名称同时查询电影和电视剧，没有类型也没有年份时使用（异步版本）。

        按共享匹配计划依次尝试年份、季信息和别名回退，首个有效候选即返回。

        :param name: 待搜索或匹配的名称
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        """
        try:
            multis = await self.search.async_multi(term=name) or []
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{str(err)}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{str(e)}")
            print(traceback.format_exc())
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")

        if not multis:
            logger.debug(f"{name} 未找到相关媒体息!")
            return {}
        plan = self._build_multi_candidate_plan(name, multis)
        resolved = await self._resolve_details_async(self._multi_resolution(name, plan))
        return self._convert_media_type(resolved)

    async def async_get_info(
        self, mtype: Optional[MediaType], tmdbid: int, raise_on_connection_error: bool = False
    ) -> Optional[_MediaRecord]:
        """
        按 TMDB 标识查询详情，并补充规范类型、别名和分级。

        类型未知时同时考虑电影和电视剧；详情身份仍存在歧义时不任意选取。

        :param mtype: 电影或电视剧类型；可选入口使用 None 表示未限定
        :param tmdbid: TMDB 数字媒体标识
        :param raise_on_connection_error: True 时重新抛出连接失败；默认 False 将其作为无结果返回
        :return: 补齐 media_type 等字段的 TMDB 字典；无匹配、失败或无法消歧时返回空值
        :raises TMDbConnectionError: 开启连接错误传播且底层未取得响应
        """
        tmdb_info = await self._run_info_async(
            self._info_resolution(mtype, tmdbid),
            tmdbid,
            raise_on_connection_error,
        )
        return self._project_info(tmdb_info)

    async def async_get_tv_season_detail(self, tmdbid: int, season: int) -> _MediaRecord:
        """
        获取电视剧季的详情（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        if not self.season_obj:
            return {}
        try:
            logger.debug("正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = await self.season_obj.async_details(tv_id=tmdbid, season_num=season)
            return tmdbinfo or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    async def async_get_tv_episode_detail(self, tmdbid: int, season: int, episode: int) -> _MediaRecord:
        """
        获取电视剧集的详情（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :param episode: 目标集号，可选入口非零时表示单集
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        if not self.episode_obj:
            return {}
        try:
            logger.debug("正在查询TMDB集详情：%s，季：%s，集：%s ..." % (tmdbid, season, episode))
            tmdbinfo = await self.episode_obj.async_details(tv_id=tmdbid, season_num=season, episode_num=episode)
            return tmdbinfo or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    async def async_discover_trending(
            self, page: Optional[int] = 1, raise_exception: bool = False
    ) -> List[_MediaRecord]:
        """
        查询周趋势榜中的电影和电视剧。

        :param page: 请求页码，从 1 开始
        :param raise_exception: True 时重新抛出查询异常，False 时记录日志并返回空列表
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        :raises Exception: 开启异常传播且底层查询失败
        """
        if not self.trending:
            return []
        try:
            logger.debug(f"正在获取流行趋势：page={page} ...")
            tmdbinfo = await self.trending.async_all_week(page=page)
            return self._normalize_trending_infos(tmdbinfo)
        except Exception as e:
            logger.error(str(e))
            if raise_exception:
                raise
            return []

    async def async_get_movie_images(
            self, tmdbid: int, original_language: Optional[str] = None
    ) -> _MediaRecord:
        """
        获取电影的图片（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :param original_language: 媒体原始语言，加入图片语言回退列表
        :return: 图片分类字典；无结果或查询失败时为空字典
        """
        if not self.movie:
            return {}
        try:
            logger.debug(f"正在获取电影图片：{tmdbid}...")
            return await self.movie.async_images(
                movie_id=tmdbid,
                include_image_language=self._build_include_image_language(
                    original_language
                ),
            ) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    async def async_get_tv_images(
            self, tmdbid: int, original_language: Optional[str] = None
    ) -> _MediaRecord:
        """
        获取电视剧的图片（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :param original_language: 媒体原始语言，加入图片语言回退列表
        :return: 图片分类字典；无结果或查询失败时为空字典
        """
        if not self.tv:
            return {}
        try:
            logger.debug(f"正在获取电视剧图片：{tmdbid}...")
            return await self.tv.async_images(
                tv_id=tmdbid,
                include_image_language=self._build_include_image_language(
                    original_language
                ),
            ) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    async def async_get_movie_similar(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电影的相似电影（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取相似电影：{tmdbid}...")
            return await self.movie.async_similar(movie_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_tv_similar(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电视剧的相似电视剧（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取相似电视剧：{tmdbid}...")
            return await self.tv.async_similar(tv_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_movie_recommend(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电影的推荐电影（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取推荐电影：{tmdbid}...")
            return await self.movie.async_recommendations(movie_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_tv_recommend(self, tmdbid: int) -> List[_MediaRecord]:
        """
        获取电视剧的推荐电视剧（异步版本）。

        :param tmdbid: TMDB 数字媒体标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取推荐电视剧：{tmdbid}...")
            return await self.tv.async_recommendations(tv_id=tmdbid) or []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_movie_credits(self, tmdbid: int,
                                      page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取电影的演员列表（异步版本）。

        仅取 cast 演员字段，不包含 crew 工作人员；拉取完整演员表后在本地分页。

        :param tmdbid: TMDB 数字媒体标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.movie:
            return []
        try:
            logger.debug(f"正在获取电影演职人员：{tmdbid}...")
            info = await self.movie.async_credits(movie_id=tmdbid) or {}
            cast = info.get('cast') or []
            if cast:
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_tv_credits(self, tmdbid: int, page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取电视剧的演员列表（异步版本）。

        仅取 cast 演员字段，不包含 crew 工作人员；拉取完整演员表后在本地分页。

        :param tmdbid: TMDB 数字媒体标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取电视剧演职人员：{tmdbid}...")
            info = await self.tv.async_credits(tv_id=tmdbid) or {}
            cast = info.get('cast') or []
            if cast:
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_tv_group_seasons(self, group_id: str) -> List[_MediaRecord]:
        """
        获取电视剧剧集组季集列表（异步版本）。

        保留剧集组顺序，并将每组 episode_number 重新编号为从 1 开始。

        :param group_id: TMDB 剧集组标识
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.tv:
            return []
        try:
            logger.debug(f"正在获取剧集组：{group_id}...")
            group_seasons = await self.tv.async_group_episodes(group_id) or []
            return [
                {
                    **group_season,
                    "episodes": [
                        {**ep, "episode_number": idx}
                        # 剧集组中每个季的episode_number从1开始
                        for idx, ep in enumerate(group_season.get("episodes", []), start=1)
                    ]
                }
                for group_season in group_seasons
            ]
        except Exception as e:
            logger.error(str(e))
            return []

    async def async_get_tv_group_detail(self, group_id: str, season: int) -> _MediaRecord:
        """
        获取剧集组某个季的信息（异步版本）。

        :param group_id: TMDB 剧集组标识
        :param season: 目标季号，0 表示特别篇，None 在可选入口表示整剧
        :return: 目标季、单集或分组详情字典；无结果或查询失败时为空字典
        """
        group_seasons = await self.async_get_tv_group_seasons(group_id)
        if not group_seasons:
            return {}
        for group_season in group_seasons:
            if group_season.get('order') == season:
                return group_season
        return {}

    async def async_get_person_detail(self, person_id: int) -> _MediaRecord:
        """
        查询人物详情。

        :param person_id: TMDB 人物标识
        :return: 人物详情字典；无结果或查询失败时为空字典
        """
        if not self.person:
            return {}
        try:
            logger.debug(f"正在获取人物详情：{person_id}...")
            return await self.person.async_details(person_id=person_id) or {}
        except Exception as e:
            logger.error(str(e))
            return {}

    async def async_get_person_credits(self, person_id: int,
                                       page: Optional[int] = 1, count: Optional[int] = 24) -> List[_MediaRecord]:
        """
        获取人物参演作品（异步版本）。

        合并电影与电视剧的 cast 作品，按上映或首播日期倒序后在本地分页。

        :param person_id: TMDB 人物标识
        :param page: 请求页码，从 1 开始
        :param count: 每页最多返回条数，演职员和作品在本地切片分页
        :return: 当前请求的结果列表；无结果或查询失败时为空列表
        """
        if not self.person:
            return []
        try:
            logger.debug(f"正在获取人物参演作品：{person_id}...")
            movies = await self.person.async_movie_credits(person_id=person_id) or {}
            tvs = await self.person.async_tv_credits(person_id=person_id) or {}
            cast = (movies.get('cast') or []) + (tvs.get('cast') or [])
            if cast:
                cast = sorted(cast, key=lambda x: x.get('release_date') or x.get('first_air_date') or '1900-01-01',
                              reverse=True)
                return cast[((1 if page is None else page) - 1) * (24 if count is None else count): (1 if page is None else page) * (24 if count is None else count)]
            return []
        except Exception as e:
            logger.error(str(e))
            return []

    def close(self) -> None:
        """
        依次关闭各 TMDB 客户端的同步会话。
        """
        for client in (
            self.tmdb,
            self.search,
            self.movie,
            self.tv,
            self.season_obj,
            self.episode_obj,
            self.discover,
            self.trending,
            self.person,
            self.collection,
        ):
            if client:
                client.close()
