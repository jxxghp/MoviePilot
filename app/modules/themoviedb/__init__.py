from typing import Optional, List, Tuple, Union

from app.core import settings, MediaInfo
from app.core.meta import MetaBase
from app.modules import _ModuleBase
from app.modules.themoviedb.category import CategoryHelper
from app.modules.themoviedb.tmdb import TmdbHelper
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.utils.types import MediaType


class TheMovieDb(_ModuleBase):
    """
    TMDB媒体信息匹配
    """

    # 元数据缓存
    cache: TmdbCache = None
    # TMDB
    tmdb: TmdbHelper = None
    # 二级分类
    category: CategoryHelper = None

    def init_module(self) -> None:
        self.cache = TmdbCache()
        self.tmdb = TmdbHelper()
        self.category = CategoryHelper()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def recognize_media(self, meta: MetaBase,
                        tmdbid: str = None) -> Optional[MediaInfo]:
        """
       识别媒体信息
       :param meta:     识别的元数据
       :param tmdbid:   tmdbid
       :return: 识别的媒体信息，包括剧集信息
       """
        if not meta:
            return None
        cache_info = self.cache.get(meta)
        if not cache_info:
            # 缓存没有或者强制不使用缓存
            if tmdbid:
                # 直接查询详情
                info = self.tmdb.get_tmdb_info(mtype=meta.type, tmdbid=tmdbid)
            else:
                if meta.type != MediaType.TV and not meta.year:
                    info = self.tmdb.search_multi_tmdb(meta.get_name())
                else:
                    if meta.type == MediaType.TV:
                        # 确定是电视
                        info = self.tmdb.search_tmdb(name=meta.get_name(),
                                                     year=meta.year,
                                                     mtype=meta.type,
                                                     season_year=meta.year,
                                                     season_number=meta.begin_season
                                                     )
                        if meta.year:
                            # 非严格模式下去掉年份再查一次
                            info = self.tmdb.search_tmdb(name=meta.get_name(),
                                                         mtype=meta.type)
                    else:
                        # 有年份先按电影查
                        info = self.tmdb.search_tmdb(name=meta.get_name(),
                                                     year=meta.year,
                                                     mtype=MediaType.MOVIE)
                        # 没有再按电视剧查
                        if not info:
                            info = self.tmdb.search_tmdb(name=meta.get_name(),
                                                         year=meta.year,
                                                         mtype=MediaType.TV
                                                         )
                        if not info:
                            # 非严格模式下去掉年份和类型再查一次
                            info = self.tmdb.search_multi_tmdb(name=meta.get_name())

                if not info:
                    # 从网站查询
                    info = self.tmdb.search_tmdb_web(name=meta.get_name(),
                                                     mtype=meta.type)
                # 补充全量信息
                if info and not info.get("genres"):
                    info = self.tmdb.get_tmdb_info(mtype=info.get("media_type"),
                                                   tmdbid=info.get("id"))
            # 保存到缓存
            self.cache.update(meta, info)
        else:
            # 使用缓存信息
            if cache_info.get("title"):
                info = self.tmdb.get_tmdb_info(mtype=cache_info.get("type"),
                                               tmdbid=cache_info.get("id"))
            else:
                info = None
        # 赋值TMDB信息并返回
        mediainfo = MediaInfo(tmdb_info=info)
        # 确定二级分类
        if info:
            if info.get('media_type') == MediaType.MOVIE:
                cat = self.category.get_movie_category(info)
            else:
                cat = self.category.get_tv_category(info)
            mediainfo.set_category(cat)

        return mediainfo

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        # 未启用时返回None
        if settings.SEARCH_SOURCE != "themoviedb":
            return None

        if not meta.get_name():
            return []
        if not meta.type and not meta.year:
            results = self.tmdb.search_multi_tmdbinfos(meta.get_name())
        else:
            if not meta.type:
                results = list(
                    set(self.tmdb.search_movie_tmdbinfos(meta.get_name(), meta.year))
                    .union(set(self.tmdb.search_tv_tmdbinfos(meta.get_name(), meta.year)))
                )
                # 组合结果的情况下要排序
                results = sorted(
                    results,
                    key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                    reverse=True
                )
            elif meta.type == MediaType.MOVIE:
                results = self.tmdb.search_movie_tmdbinfos(meta.get_name(), meta.year)
            else:
                results = self.tmdb.search_tv_tmdbinfos(meta.get_name(), meta.year)

        return [MediaInfo(tmdb_info=info) for info in results]

    def scrape_metadata(self, path: str, mediainfo: MediaInfo) -> None:
        """
        TODO 刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        if settings.SCRAP_SOURCE != "themoviedb":
            return None
