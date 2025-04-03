import re
from typing import Optional, List, Tuple, Union, Dict

import cn2an
import zhconv

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.modules.themoviedb.category import CategoryHelper
from app.modules.themoviedb.scraper import TmdbScraper
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.schemas import MediaPerson
from app.schemas.types import MediaType, MediaImageType, ModuleType, MediaRecognizeType
from app.utils.http import RequestUtils


class TheMovieDbModule(_ModuleBase):
    """
    TMDB媒体信息匹配
    """

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
        self.scraper = TmdbScraper(self.tmdb)

    @staticmethod
    def get_name() -> str:
        return "TheMovieDb"

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

    def stop(self):
        self.cache.save()
        self.tmdb.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils(proxies=settings.PROXY).get_res(
            f"https://{settings.TMDB_API_DOMAIN}/3/movie/550?api_key={settings.TMDB_API_KEY}")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接 {settings.TMDB_API_DOMAIN}，错误码：{ret.status_code}"
        return False, f"{settings.TMDB_API_DOMAIN} 网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: Optional[bool] = True,
                        **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param episode_group:  剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        if not tmdbid and not meta:
            return None

        if meta and not tmdbid \
                and settings.RECOGNIZE_SOURCE != "themoviedb":
            return None

        if not meta:
            # 未提供元数据时，直接使用tmdbid查询，不使用缓存
            cache_info = {}
        elif not meta.name:
            logger.warn("识别媒体信息时未提供元数据名称")
            return None
        else:
            # 读取缓存
            if mtype:
                meta.type = mtype
            if tmdbid:
                meta.tmdbid = tmdbid
            cache_info = self.cache.get(meta)

        # 查询剧集组
        group_episodes = []
        if episode_group:
            group_episodes = self.tmdb.get_tv_group_episodes(episode_group)

        # 识别匹配
        if not cache_info or not cache:
            info = None
            # 缓存没有或者强制不使用缓存
            if tmdbid:
                # 直接查询详情
                info = self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid)
            if not info and meta:
                info = {}
                # 简体名称
                zh_name = zhconv.convert(meta.cn_name, "zh-hans") if meta.cn_name else None
                # 使用中英文名分别识别，去重去空，但要保持顺序
                names = list(dict.fromkeys([k for k in [meta.cn_name, zh_name, meta.en_name] if k]))
                for name in names:
                    if meta.begin_season:
                        logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
                    else:
                        logger.info(f"正在识别 {name} ...")
                    if meta.type == MediaType.UNKNOWN and not meta.year:
                        info = self.tmdb.match_multi(name)
                    else:
                        if meta.type == MediaType.TV:
                            # 确定是电视
                            info = self.tmdb.match(name=name,
                                                   year=meta.year,
                                                   mtype=meta.type,
                                                   season_year=meta.year,
                                                   season_number=meta.begin_season,
                                                   group_episodes=group_episodes)
                            if not info:
                                # 去掉年份再查一次
                                info = self.tmdb.match(name=name,
                                                       mtype=meta.type)
                        else:
                            # 有年份先按电影查
                            info = self.tmdb.match(name=name,
                                                   year=meta.year,
                                                   mtype=MediaType.MOVIE)
                            # 没有再按电视剧查
                            if not info:
                                info = self.tmdb.match(name=name,
                                                       year=meta.year,
                                                       mtype=MediaType.TV,
                                                       group_episodes=group_episodes)
                            if not info:
                                # 去掉年份和类型再查一次
                                info = self.tmdb.match_multi(name=name)

                    if not info:
                        # 从网站查询
                        info = self.tmdb.match_web(name=name,
                                                   mtype=meta.type)
                    if info:
                        # 查到就退出
                        break
                # 补充全量信息
                if info and not info.get("genres"):
                    info = self.tmdb.get_info(mtype=info.get("media_type"),
                                              tmdbid=info.get("id"))
            elif not info:
                logger.error("识别媒体信息时未提供元数据或唯一且有效的tmdbid")
                return None

            # 保存到缓存
            if meta:
                self.cache.update(meta, info)
        else:
            # 使用缓存信息
            if cache_info.get("title"):
                logger.info(f"{meta.name} 使用TMDB识别缓存：{cache_info.get('title')}")
                info = self.tmdb.get_info(mtype=cache_info.get("type"),
                                          tmdbid=cache_info.get("id"))
            else:
                logger.info(f"{meta.name} 使用TMDB识别缓存：无法识别")
                info = None

        if info:
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

            # 使用剧集组的集信息和年份
            if mediainfo.type == MediaType.TV and mediainfo.episode_groups:
                if group_episodes:
                    # 指定剧集组时
                    seasons = {}
                    season_info = []
                    season_years = {}
                    for group_episode in group_episodes:
                        # 季
                        season = group_episode.get("order")
                        # 集列表
                        episodes = group_episode.get("episodes")
                        if not episodes:
                            continue
                        seasons[season] = [ep.get("episode_number") for ep in episodes]
                        season_info.append(group_episode)
                        # 当前季第一季时间
                        first_date = episodes[0].get("air_date")
                        if re.match(r"^\d{4}-\d{2}-\d{2}$", first_date):
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
                    mediainfo.episode_groups = group_episodes
                else:
                    # 每季年份
                    season_years = {}
                    for group in mediainfo.episode_groups:
                        if group.get('type') != 6:
                            # 只处理剧集部分
                            continue
                        group_episodes = self.tmdb.get_tv_group_episodes(group.get('id'))
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
                            if re.match(r"^\d{4}-\d{2}-\d{2}$", first_date):
                                season_years[season] = str(first_date).split("-")[0]
                    if season_years:
                        mediainfo.season_years = season_years
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
        :param season:  季号
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

    def tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season:  季号
        :return: TVDB信息
        """
        if not season:
            return self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid)
        else:
            return self.tmdb.get_tv_season_detail(tmdbid=tmdbid, season=season)

    def media_category(self) -> Optional[Dict[str, list]]:
        """
        获取媒体分类
        :return: 获取二级分类配置字典项，需包括电影、电视剧
        """
        return {
            MediaType.MOVIE.value: list(self.category.movie_categorys),
            MediaType.TV.value: list(self.category.tv_categorys)
        }

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息列表
        """
        if settings.SEARCH_SOURCE and "themoviedb" not in settings.SEARCH_SOURCE:
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
        if results:
            medias = [MediaInfo(tmdb_info=info) for info in results]
            if meta.begin_season:
                # 小写数据转大写
                season_str = cn2an.an2cn(meta.begin_season, "low")
                for media in medias:
                    if media.type == MediaType.TV:
                        media.title = f"{media.title} 第{season_str}季"
                        media.season = meta.begin_season
            return medias
        return []

    def search_persons(self, name: str) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        """
        if not name:
            return []
        results = self.tmdb.search_persons(name)
        if results:
            return [MediaPerson(source='themoviedb', **person) for person in results]
        return []

    def search_collections(self, name: str) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        """
        if not name:
            return []
        results = self.tmdb.search_collections(name)
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
        if settings.SCRAP_SOURCE != "themoviedb":
            return None
        return self.scraper.get_metadata_nfo(meta=meta, mediainfo=mediainfo, season=season, episode=episode)

    def metadata_img(self, mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if settings.SCRAP_SOURCE != "themoviedb":
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

    def tmdb_seasons(self, tmdbid: int) -> List[schemas.TmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息
        :param tmdbid:  TMDBID
        """
        tmdb_info = self.tmdb.get_info(tmdbid=tmdbid, mtype=MediaType.TV)
        if not tmdb_info:
            return []
        return [schemas.TmdbSeason(**season)
                for season in tmdb_info.get("seasons", []) if season.get("season_number")]

    def tmdb_episodes(self, tmdbid: int, season: int, episode_group: Optional[str] = None) -> List[schemas.TmdbEpisode]:
        """
        根据TMDBID查询某季的所有信信息
        :param tmdbid:  TMDBID
        :param season:  季
        :param episode_group:  剧集组
        """
        if episode_group:
            season_info = self.tmdb.get_tv_group_episodes(episode_group)
        else:
            season_info = self.tmdb.get_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info or not season_info.get("episodes"):
            return []
        return [schemas.TmdbEpisode(**episode) for episode in season_info.get("episodes")]

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        self.cache.save()

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if settings.RECOGNIZE_SOURCE != "themoviedb":
            return None
        if not mediainfo.tmdb_id:
            return mediainfo
        if mediainfo.logo_path \
                and mediainfo.poster_path \
                and mediainfo.backdrop_path:
            # 没有图片缺失
            return mediainfo
        # 调用TMDB图片接口
        if mediainfo.type == MediaType.MOVIE:
            images = self.tmdb.get_movie_images(mediainfo.tmdb_id)
        else:
            images = self.tmdb.get_tv_images(mediainfo.tmdb_id)
        if not images:
            return mediainfo
        if isinstance(images, list):
            images = images[0]
        # 背景图
        if not mediainfo.backdrop_path:
            backdrops = images.get("backdrops")
            if backdrops:
                backdrops = sorted(backdrops, key=lambda x: x.get("vote_average"), reverse=True)
                mediainfo.backdrop_path = backdrops[0].get("file_path")
        # 标志
        if not mediainfo.logo_path:
            logos = images.get("logos")
            if logos:
                logos = sorted(logos, key=lambda x: x.get("vote_average"), reverse=True)
                mediainfo.logo_path = logos[0].get("file_path")
        # 海报
        if not mediainfo.poster_path:
            posters = images.get("posters")
            if posters:
                posters = sorted(posters, key=lambda x: x.get("vote_average"), reverse=True)
                mediainfo.poster_path = posters[0].get("file_path")
        return mediainfo

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
            return f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/{image_prefix}{image_path}"
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

    def tmdb_movie_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = self.tmdb.get_movie_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [schemas.MediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    def tmdb_tv_credits(self, tmdbid: int, page: Optional[int] = 1) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电视剧演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        credit_infos = self.tmdb.get_tv_credits(tmdbid=tmdbid, page=page)
        if credit_infos:
            return [schemas.MediaPerson(source="themoviedb", **info) for info in credit_infos]
        return []

    def tmdb_person_detail(self, person_id: int) -> schemas.MediaPerson:
        """
        根据TMDBID查询人物详情
        :param person_id:  人物ID
        """
        detail = self.tmdb.get_person_detail(person_id=person_id)
        if detail:
            return schemas.MediaPerson(source="themoviedb", **detail)
        return schemas.MediaPerson()

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

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info("开始清除TMDB缓存 ...")
        self.tmdb.clear_cache()
        self.cache.clear()
        logger.info("TMDB缓存清除完成")
