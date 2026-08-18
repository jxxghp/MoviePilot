import re
from typing import Any, Optional, List, Tuple, Union, Dict

import cn2an

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.tmdb import TmdbSeason as _SchemaTmdbSeason
from app.schemas.tmdb import TmdbEpisode as _SchemaTmdbEpisode
from app.runtime.config import settings
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.modules.themoviedb.category import CategoryHelper
from app.modules.themoviedb.scraper import TmdbScraper
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbConnectionError
from app.schemas.category import CategoryConfig
from app.schemas.types import (
    MediaImageType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
)
from app.adapters.network.http import RequestUtils
from app.domain.media import is_media_source_enabled, is_media_source_selected
from app.schemas.media import normalize_media_source
from app.foundation.text import convert as zhconv_convert


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 榜单标识到本模块方法名的映射，discover_board 只接受在册标识，白名单校验先于 getattr 完成
_DISCOVER_BOARDS = {
    "trending": "tmdb_trending",
}


class TheMovieDbModule(_ModuleBase):
    """
    TMDB媒体信息匹配
    """
    CONFIG_WATCH = {"PROXY_HOST", "TMDB_API_DOMAIN", "TMDB_API_KEY", "TMDB_LOCALE"}

    # 元数据缓存
    cache: TmdbCache = None
    # TMDB
    tmdb: TmdbApi = None
    # 二级分类
    category: CategoryHelper = None
    # 刮削器
    scraper: TmdbScraper = None

    def init_module(self) -> None:
        self.cache = TmdbCache()
        self.tmdb = TmdbApi()
        self.category = CategoryHelper()
        self.scraper = TmdbScraper()

    @staticmethod
    def get_name() -> str:
        return "TheMovieDb"

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
        ret = RequestUtils(ua=settings.NORMAL_USER_AGENT, proxies=settings.PROXY).get_res(
            f"https://{settings.TMDB_API_DOMAIN}/3/movie/550?api_key={settings.TMDB_API_KEY}")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接 {settings.TMDB_API_DOMAIN}，错误码：{ret.status_code}"
        return False, f"{settings.TMDB_API_DOMAIN} 网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def _validate_recognize_params(
        meta: MetaBase,
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

        selected_source = normalize_media_source(media_source or settings.RECOGNIZE_SOURCE)
        if meta and not tmdbid and selected_source != MediaSource.TMDB:
            return False

        if meta and not meta.name and not tmdbid:
            logger.warn("识别媒体信息时未提供元数据名称")
            return False

        return True

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
    def _fill_group_season_info(mediainfo: MediaInfo, episode_group: Optional[str],
                                group_seasons: List[dict]) -> None:
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

    def _get_info_by_tmdbid(self, tmdbid: int, mtype: Optional[MediaType],
                             meta: Optional[MetaBase]) -> Optional[dict]:
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
        if info_tv and info_movie:
            # 同时存在，尝试通过元数据消歧
            result = self._disambiguate_by_meta(info_tv, info_movie, meta)
            if result:
                return result
            logger.warn(f"无法判断tmdb_id:{tmdbid} 是电影还是电视剧")
            return None
        if info_tv or info_movie:
            return info_tv or info_movie
        if tv_conn_error or movie_conn_error:
            raise TMDbConnectionError(f"连接TheMovieDb失败，无法确认tmdb_id:{tmdbid} 的媒体类型")
        return None

    async def _async_get_info_by_tmdbid(self, tmdbid: int, mtype: Optional[MediaType],
                                         meta: Optional[MetaBase]) -> Optional[dict]:
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
        if info_tv and info_movie:
            # 同时存在，尝试通过元数据消歧
            result = self._disambiguate_by_meta(info_tv, info_movie, meta)
            if result:
                return result
            logger.warn(f"无法判断tmdb_id:{tmdbid} 是电影还是电视剧")
            return None
        if info_tv or info_movie:
            return info_tv or info_movie
        if tv_conn_error or movie_conn_error:
            raise TMDbConnectionError(f"连接TheMovieDb失败，无法确认tmdb_id:{tmdbid} 的媒体类型")
        return None

    @staticmethod
    def _disambiguate_by_meta(info_tv: dict, info_movie: dict,
                               meta: Optional[MetaBase]) -> Optional[dict]:
        """
        通过元数据（标题、年份、类型）对同tmdbid的电影和电视剧进行消歧
        """
        if not meta:
            return None

        def _collect_titles(info: dict) -> set:
            titles = set()
            for key in ('title', 'name', 'original_title', 'original_name'):
                if info.get(key):
                    titles.add(info[key])
            for name in (info.get('names') or []):
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
                release_date = info.get('release_date') or info.get('first_air_date') or ''
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

    def _search_by_name(self, name: str, meta: MetaBase, group_seasons: List[dict]) -> dict:
        """
        根据名称搜索媒体信息
        """
        if meta.begin_season is not None:
            logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
        else:
            logger.info(f"正在识别 {name} ...")

        if meta.type == MediaType.UNKNOWN and not meta.year:
            return self.tmdb.match_multi(name)
        else:
            if meta.type == MediaType.TV:
                # 确定是电视
                info = self.tmdb.match(name=name,
                                       year=meta.year,
                                       mtype=meta.type,
                                       season_year=meta.year,
                                       season_number=meta.begin_season,
                                       group_seasons=group_seasons)
                if not info:
                    # 去掉年份再查一次
                    info = self.tmdb.match(name=name, mtype=meta.type)
                return info
            else:
                # 有年份先按电影查
                info = self.tmdb.match(name=name, year=meta.year, mtype=MediaType.MOVIE)
                # 没有再按电视剧查
                if not info:
                    info = self.tmdb.match(name=name, year=meta.year, mtype=MediaType.TV,
                                           group_seasons=group_seasons)
                if not info:
                    # 去掉年份和类型再查一次
                    info = self.tmdb.match_multi(name=name)
                return info

    async def _async_search_by_name(self, name: str, meta: MetaBase, group_seasons: List[dict]) -> dict:
        """
        根据名称搜索媒体信息（异步版本）
        """
        if meta.begin_season is not None:
            logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
        else:
            logger.info(f"正在识别 {name} ...")

        if meta.type == MediaType.UNKNOWN and not meta.year:
            return await self.tmdb.async_match_multi(name)
        else:
            if meta.type == MediaType.TV:
                # 确定是电视
                info = await self.tmdb.async_match(name=name,
                                                   year=meta.year,
                                                   mtype=meta.type,
                                                   season_year=meta.year,
                                                   season_number=meta.begin_season,
                                                   group_seasons=group_seasons)
                if not info:
                    # 去掉年份再查一次
                    info = await self.tmdb.async_match(name=name, mtype=meta.type)
                return info
            else:
                # 有年份先按电影查
                info = await self.tmdb.async_match(name=name, year=meta.year, mtype=MediaType.MOVIE)
                # 没有再按电视剧查
                if not info:
                    info = await self.tmdb.async_match(name=name, year=meta.year, mtype=MediaType.TV,
                                                       group_seasons=group_seasons)
                if not info:
                    # 去掉年份和类型再查一次
                    info = await self.tmdb.async_match_multi(name=name)
                return info

    def _process_episode_groups(self, mediainfo: MediaInfo, episode_group: Optional[str],
                                group_seasons: List[dict]) -> MediaInfo:
        """
        处理剧集组信息
        """
        if mediainfo.type == MediaType.TV and mediainfo.episode_groups:
            if group_seasons:
                self._fill_group_season_info(mediainfo, episode_group, group_seasons)
            else:
                # 每季年份
                season_years = {}
                for group in mediainfo.episode_groups:
                    if group.get('type') != 6:
                        # 只处理剧集部分
                        continue
                    group_episodes = self.tmdb.get_tv_group_seasons(group.get('id'))
                    if not group_episodes:
                        continue
                    for group_episode in group_episodes:
                        season = group_episode.get('order')
                        episodes = group_episode.get('episodes')
                        if not episodes:
                            continue
                        # 当前季第一季时间
                        first_date = episodes[0].get("air_date")
                        # 判断是不是日期格式
                        if first_date and _DATE_RE.match(first_date):
                            season_years[season] = str(first_date).split("-")[0]
                if season_years:
                    mediainfo.season_years = season_years
        return mediainfo

    async def _async_process_episode_groups(self, mediainfo: MediaInfo, episode_group: Optional[str],
                                            group_seasons: List[dict]) -> MediaInfo:
        """
        处理剧集组信息（异步版本）
        """
        if mediainfo.type == MediaType.TV and mediainfo.episode_groups:
            if group_seasons:
                self._fill_group_season_info(mediainfo, episode_group, group_seasons)
            else:
                # 每季年份
                season_years = {}
                for group in mediainfo.episode_groups:
                    if group.get('type') != 6:
                        # 只处理剧集部分
                        continue
                    group_episodes = await self.tmdb.async_get_tv_group_seasons(group.get('id'))
                    if not group_episodes:
                        continue
                    for group_episode in group_episodes:
                        season = group_episode.get('order')
                        episodes = group_episode.get('episodes')
                        if not episodes:
                            continue
                        # 当前季第一季时间
                        first_date = episodes[0].get("air_date")
                        # 判断是不是日期格式
                        if first_date and _DATE_RE.match(first_date):
                            season_years[season] = str(first_date).split("-")[0]
                if season_years:
                    mediainfo.season_years = season_years
        return mediainfo

    def _build_media_info_result(self, info: dict, meta: MetaBase, tmdbid: Optional[int],
                                 episode_group: Optional[str], group_seasons: List[dict]) -> MediaInfo:
        """
        构建MediaInfo结果
        """
        # 确定二级分类
        if info.get('media_type') == MediaType.TV:
            cat = self.category.get_tv_category(info)
        else:
            cat = self.category.get_movie_category(info)

        # 赋值TMDB信息并返回
        mediainfo = MediaInfo(tmdb_info=info)
        mediainfo.set_category(cat)

        if meta:
            logger.info(f"{meta.name} TMDB识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year} "
                        f"{mediainfo.tmdb_id}")
        else:
            logger.info(f"{tmdbid} TMDB识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")

        # 处理剧集组信息
        return self._process_episode_groups(mediainfo, episode_group, group_seasons)

    async def _async_build_media_info_result(self, info: dict, meta: MetaBase, tmdbid: Optional[int],
                                             episode_group: Optional[str], group_seasons: List[dict]) -> MediaInfo:
        """
        构建MediaInfo结果（异步版本）
        """
        # 确定二级分类
        if info.get('media_type') == MediaType.TV:
            cat = self.category.get_tv_category(info)
        else:
            cat = self.category.get_movie_category(info)

        # 赋值TMDB信息并返回
        mediainfo = MediaInfo(tmdb_info=info)
        mediainfo.set_category(cat)

        if meta:
            logger.info(f"{meta.name} TMDB识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year} "
                        f"{mediainfo.tmdb_id}")
        else:
            logger.info(f"{tmdbid} TMDB识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")

        # 处理剧集组信息
        return await self._async_process_episode_groups(mediainfo, episode_group, group_seasons)

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        media_source: Optional[MediaSource] = None,
                        media_id: Optional[str] = None,
                        episode_group: Optional[str] = None,
                        cache: Optional[bool] = True,
                        **kwargs) -> Optional[MediaInfo]:
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
        # TMDB 只处理影视；音乐识别模块异常时也不能把音乐请求回退成电视剧搜索。
        if mtype == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.TMDB:
            return None
        if media_id is not None and (
                media_source != MediaSource.TMDB or not str(media_id).isdigit()
        ):
            return None
        tmdbid = int(media_id) if media_id is not None else None
        # 验证参数
        if not self._validate_recognize_params(meta, tmdbid, media_source):
            return None

        if not meta:
            # 未提供元数据时，直接使用tmdbid查询，不使用缓存
            cache_info = {}
        else:
            # 读取缓存
            if mtype:
                meta.type = mtype
            if tmdbid:
                meta.media_source = MediaSource.TMDB
                meta.media_id = str(tmdbid)
            cache_info = self.cache.get(meta) if cache else {}

        # 查询剧集组
        group_seasons = []
        if episode_group:
            group_seasons = self.tmdb.get_tv_group_seasons(episode_group)
        cache_hit = False

        # 识别匹配
        if not cache_info or not cache:
            info = None
            connection_error = False
            # 缓存没有或者强制不使用缓存
            if tmdbid:
                # 直接查询详情，支持同ID电影/电视剧消歧
                try:
                    info = self._get_info_by_tmdbid(tmdbid=tmdbid, mtype=mtype, meta=meta)
                except TMDbConnectionError as err:
                    logger.error(f"tmdb_id:{tmdbid} {err}")
                    connection_error = True
            if not info and meta and not tmdbid:
                # 准备搜索名称
                names = self._prepare_search_names(meta)
                for name in names:
                    info = self._search_by_name(name, meta, group_seasons)
                    if info:
                        # 查到就退出
                        break
                # 补充全量信息
                if info and not info.get("genres"):
                    info = self.tmdb.get_info(mtype=info.get("media_type"),
                                              tmdbid=info.get("id"))
            elif not info:
                if connection_error:
                    # 网络故障与"条目不存在"是完全不同的两类问题，不能用同一句文案掩盖，
                    # 否则用户无从判断该等网络恢复还是该确认条目本身是否存在
                    logger.error(f"tmdb_id:{tmdbid} 连接TheMovieDb失败，无法完成识别，请检查网络连接后重试")
                elif tmdbid:
                    logger.warn(f"tmdb_id:{tmdbid} 无法确定媒体类型，识别失败")
                else:
                    logger.error("识别媒体信息时未提供元数据或唯一且有效的tmdbid")
                return None

            # 保存到缓存
            if meta:
                self.cache.update(meta, info)
        else:
            # 使用缓存信息
            cache_hit = True
            if cache_info.get("title"):
                logger.info(f"{meta.name} 使用TMDB识别缓存：{cache_info.get('title')}")
                info = self.tmdb.get_info(mtype=cache_info.get("type"),
                                          tmdbid=cache_info.get("id"))
            else:
                logger.info(f"{meta.name} 使用TMDB识别缓存：无法识别")
                info = None

        if info:
            mediainfo = self._build_media_info_result(info, meta, tmdbid, episode_group, group_seasons)
            if mediainfo:
                mediainfo.recognize_cache_hit = cache_hit
            return mediainfo
        else:
            logger.info(f"{meta.name if meta else tmdbid} 未匹配到TMDB媒体信息")

        return None

    async def async_recognize_media(self, meta: MetaBase = None,
                                    mtype: MediaType = None,
                                    media_source: Optional[MediaSource] = None,
                                    media_id: Optional[str] = None,
                                    episode_group: Optional[str] = None,
                                    cache: Optional[bool] = True,
                                    **kwargs) -> Optional[MediaInfo]:
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
        # 与同步入口保持同一类型边界，音乐请求不得进入 TMDB。
        if mtype == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.TMDB:
            return None
        if media_id is not None and (
                media_source != MediaSource.TMDB or not str(media_id).isdigit()
        ):
            return None
        tmdbid = int(media_id) if media_id is not None else None
        # 验证参数
        if not self._validate_recognize_params(meta, tmdbid, media_source):
            return None

        if not meta:
            # 未提供元数据时，直接使用tmdbid查询，不使用缓存
            cache_info = {}
        else:
            # 读取缓存
            if mtype:
                meta.type = mtype
            if tmdbid:
                meta.media_source = MediaSource.TMDB
                meta.media_id = str(tmdbid)
            cache_info = self.cache.get(meta) if cache else {}

        # 查询剧集组
        group_seasons = []
        if episode_group:
            group_seasons = await self.tmdb.async_get_tv_group_seasons(episode_group)
        cache_hit = False

        # 识别匹配
        if not cache_info or not cache:
            info = None
            connection_error = False
            # 缓存没有或者强制不使用缓存
            if tmdbid:
                # 直接查询详情，支持同ID电影/电视剧消歧
                try:
                    info = await self._async_get_info_by_tmdbid(tmdbid=tmdbid, mtype=mtype, meta=meta)
                except TMDbConnectionError as err:
                    logger.error(f"tmdb_id:{tmdbid} {err}")
                    connection_error = True
            if not info and meta and not tmdbid:
                # 准备搜索名称
                names = self._prepare_search_names(meta)
                for name in names:
                    info = await self._async_search_by_name(name, meta, group_seasons)
                    if info:
                        # 查到就退出
                        break
                # 补充全量信息
                if info and not info.get("genres"):
                    info = await self.tmdb.async_get_info(mtype=info.get("media_type"),
                                                          tmdbid=info.get("id"))
            elif not info:
                if connection_error:
                    # 网络故障与"条目不存在"是完全不同的两类问题，不能用同一句文案掩盖，
                    # 否则用户无从判断该等网络恢复还是该确认条目本身是否存在
                    logger.error(f"tmdb_id:{tmdbid} 连接TheMovieDb失败，无法完成识别，请检查网络连接后重试")
                elif tmdbid:
                    logger.warn(f"tmdb_id:{tmdbid} 无法确定媒体类型，识别失败")
                else:
                    logger.error("识别媒体信息时未提供元数据或唯一且有效的tmdbid")
                return None

            # 保存到缓存
            if meta:
                self.cache.update(meta, info)
        else:
            # 使用缓存信息
            cache_hit = True
            if cache_info.get("title"):
                logger.info(f"{meta.name} 使用TMDB识别缓存：{cache_info.get('title')}")
                info = await self.tmdb.async_get_info(mtype=cache_info.get("type"),
                                                      tmdbid=cache_info.get("id"))
            else:
                logger.info(f"{meta.name} 使用TMDB识别缓存：无法识别")
                info = None

        if info:
            mediainfo = await self._async_build_media_info_result(info, meta, tmdbid, episode_group, group_seasons)
            if mediainfo:
                mediainfo.recognize_cache_hit = cache_hit
            return mediainfo
        else:
            logger.info(f"{meta.name if meta else tmdbid} 未匹配到TMDB媒体信息")

        return None

    def match_tmdbinfo(self, name: str, mtype: MediaType = None,
                       year: Optional[str] = None, season: Optional[int] = None) -> dict:
        """
        搜索和匹配TMDB信息
        :param name:  名称
        :param mtype:  类型
        :param year:  年份
        :param season: 用于匹配指定季，0 表示特别季
        """
        # 搜索
        logger.info(f"开始使用 名称：{name} 年份：{year} 匹配TMDB信息 ...")
        info = self.tmdb.match(name=name,
                               year=year,
                               mtype=mtype,
                               season_year=year,
                               season_number=season)
        if info and not info.get("genres"):
            info = self.tmdb.get_info(mtype=info.get("media_type"),
                                      tmdbid=info.get("id"))
        return info

    async def async_match_tmdbinfo(self, name: str, mtype: MediaType = None,
                                   year: Optional[str] = None, season: Optional[int] = None) -> dict:
        """
        异步搜索和匹配TMDB信息
        :param name:  名称
        :param mtype:  类型
        :param year:  年份
        :param season: 用于匹配指定季，0 表示特别季
        """
        # 搜索
        logger.info(f"开始使用 名称：{name} 年份：{year} 匹配TMDB信息 ...")
        info = await self.tmdb.async_match(name=name,
                                           year=year,
                                           mtype=mtype,
                                           season_year=year,
                                           season_number=season)
        if info and not info.get("genres"):
            info = await self.tmdb.async_get_info(mtype=info.get("media_type"),
                                                  tmdbid=info.get("id"))
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

    def media_category(self) -> Optional[Dict[str, list]]:
        """
        获取媒体分类
        :return: 获取二级分类配置字典项，需包括电影、电视剧
        """
        return {
            MediaType.MOVIE.value: list(self.category.movie_categorys),
            MediaType.TV.value: list(self.category.tv_categorys)
        }

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        if not is_media_source_enabled(media_source, MediaSource.TMDB):
            return None
        if not meta.name:
            return []
        if meta.type == MediaType.UNKNOWN and not meta.year:
            results = self.tmdb.search_multiis(meta.name)
        else:
            if meta.type == MediaType.UNKNOWN:
                results = self.tmdb.search_movies(meta.name, meta.year)
                results.extend(self.tmdb.search_tvs(meta.name, meta.year))
                # 组合结果的情况下要排序
                results = sorted(
                    results,
                    key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                    reverse=True
                )
            elif meta.type == MediaType.MOVIE:
                results = self.tmdb.search_movies(meta.name, meta.year)
            else:
                results = self.tmdb.search_tvs(meta.name, meta.year)
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
            return [_SchemaMediaPerson(source='themoviedb', **person) for person in results]
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
            return [_SchemaMediaPerson(source='themoviedb', **person) for person in results]
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

    def metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or settings.SCRAP_SOURCE) != "themoviedb":
            return None
        return self.scraper.get_metadata_nfo(meta=meta, mediainfo=mediainfo, season=season, episode=episode)

    def metadata_img(self, mediainfo: MediaInfo, season: Optional[int] = None,
                     episode: Optional[int] = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or settings.SCRAP_SOURCE) != "themoviedb":
            return None
        return self.scraper.get_metadata_img(mediainfo=mediainfo, season=season, episode=episode)

    def tmdb_discover(self, mtype: MediaType, sort_by: str,
                      with_genres: str,
                      with_original_language: str,
                      with_keywords: str,
                      with_watch_providers: str,
                      vote_average: float,
                      vote_count: int,
                      release_date: str,
                      page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
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
            infos = self.tmdb.discover_movies({
                "sort_by": sort_by,
                "with_genres": with_genres,
                "with_original_language": with_original_language,
                "with_keywords": with_keywords,
                "with_watch_providers": with_watch_providers,
                "vote_average.gte": vote_average,
                "vote_count.gte": vote_count,
                "release_date.gte": release_date,
                "page": page
            })
        elif mtype == MediaType.TV:
            infos = self.tmdb.discover_tvs({
                "sort_by": sort_by,
                "with_genres": with_genres,
                "with_original_language": with_original_language,
                "with_keywords": with_keywords,
                "with_watch_providers": with_watch_providers,
                "vote_average.gte": vote_average,
                "vote_count.gte": vote_count,
                "first_air_date.gte": release_date,
                "page": page
            })
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
        return [_SchemaTmdbSeason(**sea)
                for sea in tmdb_info.get("seasons", []) if sea.get("season_number") is not None]

    def tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息
        :param group_id: 剧集组ID
        """
        group_seasons = self.tmdb.get_tv_group_seasons(group_id)
        if not group_seasons:
            return []
        return [_SchemaTmdbSeason(
            season_number=sea.get("order"),
            name=sea.get("name"),
            episode_count=len(sea.get("episodes") or []),
            air_date=sea.get("episodes")[0].get("air_date") if sea.get("episodes") else None,
        ) for sea in group_seasons]

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
        if mediainfo.media_source != "themoviedb" and settings.RECOGNIZE_SOURCE != "themoviedb":
            return None
        if not mediainfo.tmdb_id:
            return mediainfo
        if mediainfo.logo_path \
                and mediainfo.poster_path \
                and mediainfo.backdrop_path:
            # 没有图片缺失
            return mediainfo
        return None

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
                mediainfo.backdrop_path = settings.TMDB_IMAGE_URL(image_path)
        # 标志
        if not mediainfo.logo_path:
            if image_path := cls._pick_best_tmdb_image(images.get("logos")):
                mediainfo.logo_path = settings.TMDB_IMAGE_URL(image_path)
        # 海报
        if not mediainfo.poster_path:
            if image_path := cls._pick_best_tmdb_image(images.get("posters")):
                mediainfo.poster_path = settings.TMDB_IMAGE_URL(image_path)
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

        # 调用TMDB图片接口
        if mediainfo.type == MediaType.MOVIE:
            images = self.tmdb.get_movie_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
            )
        else:
            images = self.tmdb.get_tv_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
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

        # 调用TMDB图片接口
        if mediainfo.type == MediaType.MOVIE:
            images = await self.tmdb.async_get_movie_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
            )
        else:
            images = await self.tmdb.async_get_tv_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
            )
        if not images:
            return mediainfo

        # 处理图片数据
        return self._process_tmdb_images(mediainfo, images)

    def obtain_specific_image(self, mediaid: Union[str, int], mtype: MediaType,
                              image_type: MediaImageType, image_prefix: Optional[str] = "w500",
                              season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
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
            return settings.TMDB_IMAGE_URL(image_path, image_prefix)
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
        if not is_media_source_enabled(media_source, MediaSource.TMDB):
            return None
        if not meta.name:
            return []
        if meta.type == MediaType.UNKNOWN and not meta.year:
            results = await self.tmdb.async_search_multiis(meta.name)
        else:
            if meta.type == MediaType.UNKNOWN:
                results = await self.tmdb.async_search_movies(meta.name, meta.year)
                results.extend(await self.tmdb.async_search_tvs(meta.name, meta.year))
                # 组合结果的情况下要排序
                results = sorted(
                    results,
                    key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                    reverse=True
                )
            elif meta.type == MediaType.MOVIE:
                results = await self.tmdb.async_search_movies(meta.name, meta.year)
            else:
                results = await self.tmdb.async_search_tvs(meta.name, meta.year)
        # 将搜索词中的季写入标题中
        return self._build_search_medias_result(meta, results)

    async def async_tmdb_discover(self, mtype: MediaType, sort_by: str,
                                  with_genres: str,
                                  with_original_language: str,
                                  with_keywords: str,
                                  with_watch_providers: str,
                                  vote_average: float,
                                  vote_count: int,
                                  release_date: str,
                                  page: Optional[int] = 1,
                                  raise_exception: bool = False) -> Optional[List[MediaInfo]]:
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
            infos = await self.tmdb.async_discover_movies({
                "sort_by": sort_by,
                "with_genres": with_genres,
                "with_original_language": with_original_language,
                "with_keywords": with_keywords,
                "with_watch_providers": with_watch_providers,
                "vote_average.gte": vote_average,
                "vote_count.gte": vote_count,
                "release_date.gte": release_date,
                "page": page
            }, raise_exception=raise_exception)
        elif mtype == MediaType.TV:
            infos = await self.tmdb.async_discover_tvs({
                "sort_by": sort_by,
                "with_genres": with_genres,
                "with_original_language": with_original_language,
                "with_keywords": with_keywords,
                "with_watch_providers": with_watch_providers,
                "vote_average.gte": vote_average,
                "vote_count.gte": vote_count,
                "first_air_date.gte": release_date,
                "page": page
            }, raise_exception=raise_exception)
        else:
            return []
        if infos:
            return [MediaInfo(tmdb_info=info) for info in infos]
        return []

    async def async_tmdb_trending(
            self, page: Optional[int] = 1, raise_exception: bool = False
    ) -> List[MediaInfo]:
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
        return [_SchemaTmdbSeason(**sea)
                for sea in tmdb_info.get("seasons", []) if sea.get("season_number") is not None]

    async def async_tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息（异步版本）
        :param group_id: 剧集组ID
        """
        group_seasons = await self.tmdb.async_get_tv_group_seasons(group_id)
        if not group_seasons:
            return []
        return [_SchemaTmdbSeason(
            season_number=sea.get("order"),
            name=sea.get("name"),
            episode_count=len(sea.get("episodes") or []),
            air_date=sea.get("episodes")[0].get("air_date") if sea.get("episodes") else None,
        ) for sea in group_seasons]

    async def async_tmdb_episodes(self, tmdbid: int, season: int,
                                  episode_group: Optional[str] = None) -> List[_SchemaTmdbEpisode]:
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

    def load_category_config(self) -> CategoryConfig:
        """
        加载分类配置
        """
        return self.category.load()

    def save_category_config(self, config: CategoryConfig) -> bool:
        """
        保存分类配置
        """
        return self.category.save(config)

    def match_media(self, source: Optional[MediaSource] = None,
                     name: str = None,
                     mtype: Optional[MediaType] = None,
                     year: Optional[str] = None,
                     season: Optional[int] = None,
                     imdbid: Optional[str] = None,
                     raise_exception: bool = False,
                     **kwargs) -> Optional[dict]:
        """
        搜索和匹配指定来源的媒体信息
        :param source: 媒体来源，非TMDB来源返回 None
        :param name: 名称
        :param mtype: 类型
        :param year: 年份
        :param season: 用于匹配指定季，0 表示特别季
        :param imdbid: 本源不支持
        :param raise_exception: 本源不支持
        :return: 匹配到的媒体信息
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return self.match_tmdbinfo(name=name, mtype=mtype, year=year, season=season)

    async def async_match_media(self, source: Optional[MediaSource] = None,
                                 name: str = None,
                                 mtype: Optional[MediaType] = None,
                                 year: Optional[str] = None,
                                 season: Optional[int] = None,
                                 imdbid: Optional[str] = None,
                                 raise_exception: bool = False,
                                 **kwargs) -> Optional[dict]:
        """
        搜索和匹配指定来源的媒体信息（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param name: 名称
        :param mtype: 类型
        :param year: 年份
        :param season: 用于匹配指定季，0 表示特别季
        :param imdbid: 本源不支持
        :param raise_exception: 本源不支持
        :return: 匹配到的媒体信息
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return await self.async_match_tmdbinfo(name=name, mtype=mtype, year=year, season=season)

    def person_detail(self, source: Optional[MediaSource] = None,
                       person_id: int = None,
                       **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情
        :param source: 媒体来源，非TMDB来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return self.tmdb_person_detail(person_id=person_id)

    async def async_person_detail(self, source: Optional[MediaSource] = None,
                                   person_id: int = None,
                                   **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return await self.async_tmdb_person_detail(person_id=person_id)

    def person_credits(self, source: Optional[MediaSource] = None,
                        person_id: int = None,
                        page: int = 1,
                        count: Optional[int] = None,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品
        :param source: 媒体来源，非TMDB来源返回 None
        :param person_id: 人物ID
        :param page: 页码
        :param count: 本源不支持
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return self.tmdb_person_credits(person_id=person_id, page=page)

    async def async_person_credits(self, source: Optional[MediaSource] = None,
                                    person_id: int = None,
                                    page: int = 1,
                                    count: Optional[int] = None,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param person_id: 人物ID
        :param page: 页码
        :param count: 本源不支持
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return await self.async_tmdb_person_credits(person_id=person_id, page=page)

    def media_credits(self, source: Optional[MediaSource] = None,
                       media_id: Any = None,
                       mtype: Optional[MediaType] = None,
                       page: int = 1,
                       count: Optional[int] = None,
                       **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :param page: 页码
        :param count: 本源不支持
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return self.tmdb_tv_credits(tmdbid=tmdbid, page=page)
        return self.tmdb_movie_credits(tmdbid=tmdbid, page=page)

    async def async_media_credits(self, source: Optional[MediaSource] = None,
                                   media_id: Any = None,
                                   mtype: Optional[MediaType] = None,
                                   page: int = 1,
                                   count: Optional[int] = None,
                                   **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :param page: 页码
        :param count: 本源不支持
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return await self.async_tmdb_tv_credits(tmdbid=tmdbid, page=page)
        return await self.async_tmdb_movie_credits(tmdbid=tmdbid, page=page)

    def media_recommend(self, source: Optional[MediaSource] = None,
                         media_id: Any = None,
                         mtype: Optional[MediaType] = None,
                         page: int = 1,
                         count: Optional[int] = None,
                         **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return self.tmdb_tv_recommend(tmdbid=tmdbid)
        return self.tmdb_movie_recommend(tmdbid=tmdbid)

    async def async_media_recommend(self, source: Optional[MediaSource] = None,
                                     media_id: Any = None,
                                     mtype: Optional[MediaType] = None,
                                     page: int = 1,
                                     count: Optional[int] = None,
                                     **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return await self.async_tmdb_tv_recommend(tmdbid=tmdbid)
        return await self.async_tmdb_movie_recommend(tmdbid=tmdbid)

    def media_similar(self, source: Optional[MediaSource] = None,
                       media_id: Any = None,
                       mtype: Optional[MediaType] = None,
                       **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相似媒体
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :return: 相似媒体列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return self.tmdb_tv_similar(tmdbid=tmdbid)
        return self.tmdb_movie_similar(tmdbid=tmdbid)

    async def async_media_similar(self, source: Optional[MediaSource] = None,
                                   media_id: Any = None,
                                   mtype: Optional[MediaType] = None,
                                   **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相似媒体（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型，TV走剧集接口，其余（含未指定）按电影处理
        :return: 相似媒体列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        if mtype == MediaType.TV:
            return await self.async_tmdb_tv_similar(tmdbid=tmdbid)
        return await self.async_tmdb_movie_similar(tmdbid=tmdbid)

    def discover(self, source: Optional[MediaSource] = None,
                 **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体
        :param source: 媒体来源，非TMDB来源返回 None
        :param criteria: 筛选条件，原样转发给 tmdb_discover，不补默认值；本源要求包含 mtype、
                         sort_by、with_genres、with_original_language、with_keywords、
                         with_watch_providers、vote_average、vote_count、release_date 等必填项与
                         可选的 page，必填项缺失时由 tmdb_discover 自身抛出异常
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return self.tmdb_discover(**criteria)

    async def async_discover(self, source: Optional[MediaSource] = None,
                              **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param criteria: 筛选条件，原样转发给 async_tmdb_discover，规则同步版本一致，
                         另支持 raise_exception（触发速率限制时是否抛出异常）
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        return await self.async_tmdb_discover(**criteria)

    def discover_board(self, source: Optional[MediaSource] = None,
                        board: str = None,
                        page: int = 1,
                        count: int = 30,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单
        :param source: 媒体来源，非TMDB来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 页码
        :param count: 本源不支持
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return getattr(self, method_name)(page=page)

    async def async_discover_board(self, source: Optional[MediaSource] = None,
                                    board: str = None,
                                    page: int = 1,
                                    count: int = 30,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 页码
        :param count: 本源不支持
        :param kwargs: 支持 raise_exception（触发速率限制时是否抛出异常）
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return await getattr(self, f"async_{method_name}")(
            page=page, raise_exception=kwargs.get("raise_exception", False)
        )

    def media_detail(self, source: Optional[MediaSource] = None,
                      media_id: Any = None,
                      mtype: Optional[MediaType] = None,
                      season: Optional[int] = None,
                      raise_exception: bool = False,
                      **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型
        :param season: 季号；TV 的显式值（含 0）读取季详情，None 或电影的 0 读取媒体详情
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.tmdb_info(tmdbid=tmdbid, mtype=mtype, season=season)

    async def async_media_detail(self, source: Optional[MediaSource] = None,
                                  media_id: Any = None,
                                  mtype: Optional[MediaType] = None,
                                  season: Optional[int] = None,
                                  raise_exception: bool = False,
                                  **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情（异步版本）
        :param source: 媒体来源，非TMDB来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 媒体类型
        :param season: 季号；TV 的显式值（含 0）读取季详情，None 或电影的 0 读取媒体详情
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.TMDB:
            return None
        if media_id is None:
            return None
        try:
            tmdbid = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_tmdb_info(tmdbid=tmdbid, mtype=mtype, season=season)
