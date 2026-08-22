from typing import Callable, List, Optional

import pillow_avif  # noqa: F401  # pylint: disable=unused-import  # AVIF 注册副作用

from app.application.orchestration.bangumi import BangumiChain
from app.application.orchestration.douban import DoubanChain
from app.application.orchestration.listenbrainz import ListenBrainzChain
from app.application.orchestration.tmdb import TmdbChain
from app.runtime.cache import cached, fresh
from app.runtime.config import global_vars
from app.domain.context import MusicInfo
from app.application.image import ImageHelper
from app.runtime.log import logger
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)
from app.runtime.execution import log_execution_time
from app.schemas.media import normalize_media_source
from app.foundation.singleton import Singleton


class RecommendChain(metaclass=Singleton):
    """
    推荐处理链，单例运行
    """

    # 推荐缓存时间
    recommend_ttl = 24 * 3600
    # 推荐缓存页数
    cache_max_pages = 5
    # 推荐缓存区域
    recommend_cache_region = "recommend"

    def music_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
            sort_by: str = "listen_count.desc",
            min_listen_count: int = 0,
            with_cover: bool = False,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """读取 ListenBrainz 音乐榜单并应用推荐筛选与排序。"""
        results = ListenBrainzChain().music_chart(
            range_name=range_name,
            page=page,
            count=count,
            entity=entity,
        )
        return self._filter_music_candidates(
            results,
            count=count,
            sort_by=sort_by,
            min_listen_count=min_listen_count,
            with_cover=with_cover,
        )

    async def async_music_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
            sort_by: str = "listen_count.desc",
            min_listen_count: int = 0,
            with_cover: bool = False,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 音乐榜单并应用推荐筛选与排序。"""
        results = await ListenBrainzChain().async_music_chart(
            range_name=range_name,
            page=page,
            count=count,
            entity=entity,
        )
        return self._filter_music_candidates(
            results,
            count=count,
            sort_by=sort_by,
            min_listen_count=min_listen_count,
            with_cover=with_cover,
        )

    async def async_music_fresh_releases(
            self,
            days: int = 14,
            sort: str = "release_date",
            past: bool = True,
            future: bool = True,
            page: int = 1,
            count: int = 30,
            with_cover: bool = False,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 新发行专辑并应用封面筛选。"""
        results = await ListenBrainzChain().async_music_fresh_releases(
            days=days,
            sort=sort,
            past=past,
            future=future,
            page=page,
            count=count,
        )
        return self._filter_music_candidates(
            results,
            count=count,
            with_cover=with_cover,
        )

    def music_discover(
            self,
            media_source: MediaSource,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """按固定音乐来源读取发现内容，当前支持豆瓣音乐。"""
        if normalize_media_source(media_source) != MediaSource.DoubanMusic:
            return []
        return DoubanChain().music_discover(
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )

    async def async_music_discover(
            self,
            media_source: MediaSource,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """异步按固定音乐来源读取发现内容，当前支持豆瓣音乐。"""
        if normalize_media_source(media_source) != MediaSource.DoubanMusic:
            return []
        return await DoubanChain().async_music_discover(
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )

    @staticmethod
    def _filter_music_candidates(
            candidates: list[MusicInfo],
            count: int,
            sort_by: Optional[str] = None,
            min_listen_count: int = 0,
            with_cover: bool = False,
    ) -> list[MusicInfo]:
        """按热度和封面条件筛选音乐推荐，并限制返回数量。"""
        results = [
            info for info in candidates
            if (info.listen_count or 0) >= max(0, min_listen_count)
            and (not with_cover or bool(info.cover_url))
        ]
        if sort_by:
            results.sort(
                key=lambda info: info.listen_count or 0,
                reverse=sort_by != "listen_count.asc",
            )
        return results[:max(1, count)]

    def refresh_recommend(
            self,
            manual: bool = False,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        刷新推荐

        :param manual: 手动触发
        :param progress_callback: 定时服务进度更新回调
        """
        logger.debug("Starting to refresh Recommend data.")

        # 推荐来源方法
        recommend_methods = [
            self.tmdb_movies,
            self.tmdb_tvs,
            self.tmdb_trending,
            self.bangumi_calendar,
            self.douban_movie_showing,
            self.douban_movies,
            self.douban_tvs,
            self.douban_movie_top250,
            self.douban_tv_weekly_chinese,
            self.douban_tv_weekly_global,
            self.douban_tv_animation,
            self.douban_movie_hot,
            self.douban_tv_hot,
            self.music_weekly,
            self.music_douban,
        ]

        # 缓存并刷新所有推荐数据
        recommends = []
        # 记录哪些方法已完成
        methods_finished = set()
        total_requests = len(recommend_methods) * self.cache_max_pages
        finished_requests = 0
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始刷新推荐缓存，共 {total_requests} 个数据分页 ...",
                data={"total": total_requests, "finished": 0},
            )
        # 这里避免区间内连续调用相同来源，因此遍历方案为每页遍历所有推荐来源，再进行页数遍历
        for page in range(1, self.cache_max_pages + 1):
            for method in recommend_methods:
                if global_vars.is_system_stopped:
                    return
                if method in methods_finished:
                    continue
                logger.debug(f"Fetch {method.__name__} data for page {page}.")
                # 手动触发的刷新，总是需要获取最新数据
                with fresh(manual):
                    data = method(page=page)
                finished_requests += 1
                if progress_callback:
                    progress_callback(
                        value=finished_requests / total_requests * 90,
                        text=(
                            f"正在刷新推荐缓存"
                            f"（{finished_requests}/{total_requests}）..."
                        ),
                        data={
                            "total": total_requests,
                            "finished": finished_requests,
                            "current": method.__name__,
                            "page": page,
                        },
                    )
                if not data:
                    logger.debug("All recommendation methods have finished fetching data. Ending pagination early.")
                    methods_finished.add(method)
                    continue
                recommends.extend(data)
            # 如果所有方法都已经完成，提前结束循环
            if len(methods_finished) == len(recommend_methods):
                break

        # 缓存收集到的海报
        if progress_callback:
            progress_callback(value=90, text="推荐数据刷新完成，正在缓存海报 ...")
        self.__cache_posters(recommends, progress_callback=progress_callback)
        logger.debug("Recommend data refresh completed.")
        if progress_callback:
            progress_callback(value=100, text="推荐缓存刷新完成")

    def __cache_posters(
            self,
            datas: List[dict],
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        提取 poster_path 并缓存图片
        :param datas: 数据列表
        :param progress_callback: 定时服务进度更新回调
        """
        if not self.runtime_config.global_image_cache:
            return

        total_num = len(datas)
        for index, data in enumerate(datas, start=1):
            if global_vars.is_system_stopped:
                return
            poster_path = data.get("poster_path")
            if poster_path:
                poster_url = poster_path.replace("original", "w500")
                self.__fetch_and_save_image(poster_url)
            if progress_callback:
                progress_callback(
                    value=90 + (index / total_num * 10 if total_num else 10),
                    text=f"正在缓存推荐海报（{index}/{total_num}）...",
                    data={"poster_total": total_num, "poster_finished": index},
                )

    @staticmethod
    def __fetch_and_save_image(url: str):
        """
        请求并保存图片
        :param url: 图片路径
        """
        ImageHelper().fetch_image(url=url)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def tmdb_movies(self, sort_by: Optional[str] = "popularity.desc",
                    with_genres: Optional[str] = "",
                    with_original_language: Optional[str] = "",
                    with_keywords: Optional[str] = "",
                    with_watch_providers: Optional[str] = "",
                    vote_average: Optional[float] = 0.0,
                    vote_count: Optional[int] = 0,
                    release_date: Optional[str] = "",
                    page: Optional[int] = 1) -> List[dict]:
        """
        TMDB热门电影
        """
        movies = TmdbChain().tmdb_discover(mtype=MediaType.MOVIE,
                                           sort_by=sort_by,
                                           with_genres=with_genres,
                                           with_original_language=with_original_language,
                                           with_keywords=with_keywords,
                                           with_watch_providers=with_watch_providers,
                                           vote_average=vote_average,
                                           vote_count=vote_count,
                                           release_date=release_date,
                                           page=page)
        return [movie.to_dict() for movie in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def music_weekly(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """返回 ListenBrainz 本周全站热门音乐。"""
        medias = self.music_chart(
            range_name="this_week",
            page=page or 1,
            count=count or 30,
        )
        return [media.to_dict() for media in medias]

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def music_douban(
            self,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
    ) -> List[dict]:
        """返回豆瓣音乐官方新碟榜。"""
        medias = self.music_discover(
            media_source=MediaSource.DoubanMusic,
            page=page or 1,
            count=count or 30,
            entity=MUSIC_ENTITY_ALBUM,
        )
        return [media.to_dict() for media in medias]

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def tmdb_tvs(self, sort_by: Optional[str] = "popularity.desc",
                 with_genres: Optional[str] = "",
                 with_original_language: Optional[str] = "zh|en|ja|ko",
                 with_keywords: Optional[str] = "",
                 with_watch_providers: Optional[str] = "",
                 vote_average: Optional[float] = 0.0,
                 vote_count: Optional[int] = 0,
                 release_date: Optional[str] = "",
                 page: Optional[int] = 1) -> List[dict]:
        """
        TMDB热门电视剧
        """
        tvs = TmdbChain().tmdb_discover(mtype=MediaType.TV,
                                        sort_by=sort_by,
                                        with_genres=with_genres,
                                        with_original_language=with_original_language,
                                        with_keywords=with_keywords,
                                        with_watch_providers=with_watch_providers,
                                        vote_average=vote_average,
                                        vote_count=vote_count,
                                        release_date=release_date,
                                        page=page)
        return [tv.to_dict() for tv in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def tmdb_trending(self, page: Optional[int] = 1) -> List[dict]:
        """
        TMDB流行趋势
        """
        infos = TmdbChain().tmdb_trending(page=page)
        return [info.to_dict() for info in infos] if infos else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def bangumi_calendar(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        Bangumi每日放送
        """
        medias = BangumiChain().calendar()
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣正在热映
        """
        movies = DoubanChain().movie_showing(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movies(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                      page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣最新电影
        """
        movies = DoubanChain().douban_discover(mtype=MediaType.MOVIE,
                                               sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tvs(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                   page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣最新电视剧
        """
        tvs = DoubanChain().douban_discover(mtype=MediaType.TV,
                                            sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣电影TOP250
        """
        movies = DoubanChain().movie_top250(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣国产剧集榜
        """
        tvs = DoubanChain().tv_weekly_chinese(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣全球剧集榜
        """
        tvs = DoubanChain().tv_weekly_global(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门动漫
        """
        tvs = DoubanChain().tv_animation(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门电影
        """
        movies = DoubanChain().movie_hot(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门电视剧
        """
        tvs = DoubanChain().tv_hot(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_tmdb_movies(self, sort_by: Optional[str] = "popularity.desc",
                                with_genres: Optional[str] = "",
                                with_original_language: Optional[str] = "",
                                with_keywords: Optional[str] = "",
                                with_watch_providers: Optional[str] = "",
                                vote_average: Optional[float] = 0.0,
                                vote_count: Optional[int] = 0,
                                release_date: Optional[str] = "",
                                page: Optional[int] = 1,
                                raise_exception: bool = False) -> List[dict]:
        """
        异步TMDB热门电影
        """
        movies = await TmdbChain().async_tmdb_discover(mtype=MediaType.MOVIE,
                                                        sort_by=sort_by,
                                                        with_genres=with_genres,
                                                        with_original_language=with_original_language,
                                                        with_keywords=with_keywords,
                                                        with_watch_providers=with_watch_providers,
                                                        vote_average=vote_average,
                                                        vote_count=vote_count,
                                                        release_date=release_date,
                                                        page=page,
                                                        raise_exception=raise_exception)
        return [movie.to_dict() for movie in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_tmdb_tvs(self, sort_by: Optional[str] = "popularity.desc",
                             with_genres: Optional[str] = "",
                             with_original_language: Optional[str] = "zh|en|ja|ko",
                             with_keywords: Optional[str] = "",
                             with_watch_providers: Optional[str] = "",
                             vote_average: Optional[float] = 0.0,
                             vote_count: Optional[int] = 0,
                             release_date: Optional[str] = "",
                             page: Optional[int] = 1,
                             raise_exception: bool = False) -> List[dict]:
        """
        异步TMDB热门电视剧
        """
        tvs = await TmdbChain().async_tmdb_discover(mtype=MediaType.TV,
                                                     sort_by=sort_by,
                                                     with_genres=with_genres,
                                                     with_original_language=with_original_language,
                                                     with_keywords=with_keywords,
                                                     with_watch_providers=with_watch_providers,
                                                     vote_average=vote_average,
                                                     vote_count=vote_count,
                                                     release_date=release_date,
                                                     page=page,
                                                     raise_exception=raise_exception)
        return [tv.to_dict() for tv in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_tmdb_trending(
            self, page: Optional[int] = 1, raise_exception: bool = False
    ) -> List[dict]:
        """
        异步TMDB流行趋势
        """
        infos = await TmdbChain().async_tmdb_trending(
            page=page,
            raise_exception=raise_exception,
        )
        return [info.to_dict() for info in infos] if infos else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_bangumi_calendar(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步Bangumi每日放送
        """
        medias = await BangumiChain().async_calendar()
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣正在热映
        """
        movies = await DoubanChain().async_movie_showing(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_music_weekly(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """异步返回 ListenBrainz 本周全站热门音乐。"""
        medias = await self.async_music_chart(
            range_name="this_week",
            page=page or 1,
            count=count or 30,
        )
        return [media.to_dict() for media in medias]

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_music_douban(
            self,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
    ) -> List[dict]:
        """异步返回豆瓣音乐官方新碟榜。"""
        medias = await self.async_music_discover(
            media_source=MediaSource.DoubanMusic,
            page=page or 1,
            count=count or 30,
            entity=MUSIC_ENTITY_ALBUM,
        )
        return [media.to_dict() for media in medias]

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movies(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                                  page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电影
        """
        movies = await DoubanChain().async_douban_discover(mtype=MediaType.MOVIE,
                                                            sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tvs(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                               page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电视剧
        """
        tvs = await DoubanChain().async_douban_discover(mtype=MediaType.TV,
                                                         sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣电影TOP250
        """
        movies = await DoubanChain().async_movie_top250(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣国产剧集榜
        """
        tvs = await DoubanChain().async_tv_weekly_chinese(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣全球剧集榜
        """
        tvs = await DoubanChain().async_tv_weekly_global(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门动漫
        """
        tvs = await DoubanChain().async_tv_animation(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电影
        """
        movies = await DoubanChain().async_movie_hot(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电视剧
        """
        tvs = await DoubanChain().async_tv_hot(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []
