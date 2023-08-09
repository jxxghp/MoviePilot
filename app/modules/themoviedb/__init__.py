from pathlib import Path
from typing import Optional, List, Tuple, Union

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.modules.themoviedb.category import CategoryHelper
from app.modules.themoviedb.scraper import TmdbScraper
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.modules.themoviedb.tmdbapi import TmdbHelper
from app.schemas.types import MediaType, MediaImageType
from app.utils.system import SystemUtils


class TheMovieDbModule(_ModuleBase):
    """
    TMDB媒体信息匹配
    """

    # 元数据缓存
    cache: TmdbCache = None
    # TMDB
    tmdb: TmdbHelper = None
    # 二级分类
    category: CategoryHelper = None
    # 刮削器
    scraper: TmdbScraper = None

    def init_module(self) -> None:
        self.cache = TmdbCache()
        self.tmdb = TmdbHelper()
        self.category = CategoryHelper()
        self.scraper = TmdbScraper(self.tmdb)

    def stop(self):
        self.cache.save()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :return: 识别的媒体信息，包括剧集信息
        """
        if not meta:
            cache_info = {}
        else:
            if mtype:
                meta.type = mtype
            cache_info = self.cache.get(meta)
        if not cache_info:
            # 缓存没有或者强制不使用缓存
            if tmdbid:
                # 直接查询详情
                info = self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid)
            elif meta:
                logger.info(f"正在识别 {meta.name} ...")
                if meta.type == MediaType.UNKNOWN and not meta.year:
                    info = self.tmdb.match_multi(meta.name)
                else:
                    if meta.type == MediaType.TV:
                        # 确定是电视
                        info = self.tmdb.match(name=meta.name,
                                               year=meta.year,
                                               mtype=meta.type,
                                               season_year=meta.year,
                                               season_number=meta.begin_season)
                        if meta.year:
                            # 非严格模式下去掉年份再查一次
                            info = self.tmdb.match(name=meta.name,
                                                   mtype=meta.type)
                    else:
                        # 有年份先按电影查
                        info = self.tmdb.match(name=meta.name,
                                               year=meta.year,
                                               mtype=MediaType.MOVIE)
                        # 没有再按电视剧查
                        if not info:
                            info = self.tmdb.match(name=meta.name,
                                                   year=meta.year,
                                                   mtype=MediaType.TV)
                        if not info:
                            # 非严格模式下去掉年份和类型再查一次
                            info = self.tmdb.match_multi(name=meta.name)

                if not info:
                    # 从网站查询
                    info = self.tmdb.match_web(name=meta.name,
                                               mtype=meta.type)
                # 补充全量信息
                if info and not info.get("genres"):
                    info = self.tmdb.get_info(mtype=info.get("media_type"),
                                              tmdbid=info.get("id"))
            else:
                logger.error("识别媒体信息时未提供元数据或tmdbid")
                return None
            # 保存到缓存
            if meta:
                self.cache.update(meta, info)
        else:
            # 使用缓存信息
            if cache_info.get("title"):
                logger.info(f"{meta.name} 使用识别缓存：{cache_info.get('title')}")
                info = self.tmdb.get_info(mtype=cache_info.get("type"),
                                          tmdbid=cache_info.get("id"))
            else:
                logger.info(f"{meta.name} 使用识别缓存：无法识别")
                info = None

        if info:
            # 确定二级分类
            if info.get('media_type') == MediaType.TV:
                cat = self.category.get_tv_category(info)
            elif info.get('media_type') == MediaType.ANIME:
                cat = self.category.get_anime_category(info)
            else:
                cat = self.category.get_movie_category(info)
            # 赋值TMDB信息并返回
            mediainfo = MediaInfo(tmdb_info=info)
            mediainfo.set_category(cat)
            if meta:
                logger.info(f"{meta.name} 识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year} "
                            f"{mediainfo.tmdb_id}")
            else:
                logger.info(f"{tmdbid} 识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year}")
            return mediainfo
        else:
            logger.info(f"{meta.name if meta else tmdbid} 未匹配到媒体信息")

        return None

    def tmdb_info(self, tmdbid: int, mtype: MediaType) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :return: TVDB信息
        """
        return self.tmdb.get_info(mtype=mtype, tmdbid=tmdbid)

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息列表
        """
        # 未启用时返回None
        if settings.SEARCH_SOURCE != "themoviedb":
            return None

        if not meta.name:
            return []
        if meta.type == MediaType.UNKNOWN and not meta.year:
            results = self.tmdb.search_multiis(meta.name)
        else:
            if meta.type == MediaType.UNKNOWN:
                results = list(
                    set(self.tmdb.search_movies(meta.name, meta.year))
                    .union(set(self.tmdb.search_tv_tmdbinfos(meta.name, meta.year)))
                )
                # 组合结果的情况下要排序
                results = sorted(
                    results,
                    key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                    reverse=True
                )
            elif meta.type == MediaType.MOVIE:
                results = self.tmdb.search_movies(meta.name, meta.year)
            else:
                results = self.tmdb.search_tv_tmdbinfos(meta.name, meta.year)

        return [MediaInfo(tmdb_info=info) for info in results]

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        if settings.SCRAP_SOURCE != "themoviedb":
            return None
        # 目录下的所有文件
        for file in SystemUtils.list_files_with_extensions(path, settings.RMT_MEDIAEXT):
            if not file:
                continue
            logger.info(f"开始刮削媒体库文件：{file} ...")
            self.scraper.gen_scraper_files(mediainfo=mediainfo,
                                           file_path=file)
            logger.info(f"{file} 刮削完成")

    def tmdb_discover(self, mtype: MediaType, sort_by: str, with_genres: str, with_original_language: str,
                      page: int = 1) -> Optional[List[dict]]:
        """
        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param page:  页码
        :return: 媒体信息列表
        """
        if mtype == MediaType.MOVIE:
            return self.tmdb.discover_movies(sort_by=sort_by,
                                             with_genres=with_genres,
                                             with_original_language=with_original_language,
                                             page=page)
        elif mtype == MediaType.TV:
            return self.tmdb.discover_tvs(sort_by=sort_by,
                                          with_genres=with_genres,
                                          with_original_language=with_original_language,
                                          page=page)
        else:
            return None

    def tmdb_trending(self, page: int = 1) -> List[dict]:
        """
        TMDB流行趋势
        :param page: 第几页
        :return: TMDB信息列表
        """
        return self.tmdb.trending.all_week(page=page)

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

    def tmdb_episodes(self, tmdbid: int, season: int) -> List[schemas.TmdbEpisode]:
        """
        根据TMDBID查询某季的所有信信息
        :param tmdbid:  TMDBID
        :param season:  季
        """
        season_info = self.tmdb.get_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info:
            return []
        return [schemas.TmdbEpisode(**episode) for episode in season_info.get("episodes", [])]

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
                              image_type: MediaImageType, image_prefix: str = "w500",
                              season: int = None, episode: int = None) -> Optional[str]:
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
        if not season and not episode:
            tmdbinfo = self.tmdb.get_info(mtype=mtype, tmdbid=int(mediaid))
            if tmdbinfo:
                image_path = tmdbinfo.get(image_type.value)
        elif season and episode:
            episodeinfo = self.tmdb.get_tv_episode_detail(tmdbid=int(mediaid), season=season, episode=episode)
            if episodeinfo:
                image_path = episodeinfo.get("still_path")
        elif season:
            seasoninfo = self.tmdb.get_tv_season_detail(tmdbid=int(mediaid), season=season)
            if seasoninfo:
                image_path = seasoninfo.get(image_type.value)

        if image_path:
            return f"https://image.tmdb.org/t/p/{image_prefix}{image_path}"
        return None

    def movie_similar(self, tmdbid: int) -> List[dict]:
        """
        根据TMDBID查询类似电影
        :param tmdbid:  TMDBID
        """
        return self.tmdb.get_movie_similar(tmdbid=tmdbid)

    def tv_similar(self, tmdbid: int) -> List[dict]:
        """
        根据TMDBID查询类似电视剧
        :param tmdbid:  TMDBID
        """
        return self.tmdb.get_tv_similar(tmdbid=tmdbid)

    def movie_recommend(self, tmdbid: int) -> List[dict]:
        """
        根据TMDBID查询推荐电影
        :param tmdbid:  TMDBID
        """
        return self.tmdb.get_movie_recommend(tmdbid=tmdbid)

    def tv_recommend(self, tmdbid: int) -> List[dict]:
        """
        根据TMDBID查询推荐电视剧
        :param tmdbid:  TMDBID
        """
        return self.tmdb.get_tv_recommend(tmdbid=tmdbid)

    def movie_credits(self, tmdbid: int, page: int = 1) -> List[dict]:
        """
        根据TMDBID查询电影演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        return self.tmdb.get_movie_credits(tmdbid=tmdbid, page=page)

    def tv_credits(self, tmdbid: int, page: int = 1) -> List[dict]:
        """
        根据TMDBID查询电视剧演职员表
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        return self.tmdb.get_tv_credits(tmdbid=tmdbid, page=page)

    def person_detail(self, person_id: int) -> dict:
        """
        根据TMDBID查询人物详情
        :param person_id:  人物ID
        """
        return self.tmdb.get_person_detail(person_id=person_id)

    def person_credits(self, person_id: int, page: int = 1) -> List[dict]:
        """
        根据TMDBID查询人物参演作品
        :param person_id:  人物ID
        :param page:  页码
        """
        return self.tmdb.get_person_credits(person_id=person_id, page=page)
