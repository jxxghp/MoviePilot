from typing import Callable, List, Optional

import pillow_avif  # noqa 用于自动注册AVIF支持

from app.chain import ChainBase
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.core.cache import cached, fresh
from app.core.config import settings, global_vars
from app.helper.image import ImageHelper
from app.log import logger
from app.schemas import MediaType
from app.utils.common import log_execution_time
from app.utils.singleton import Singleton


class RecommendChain(ChainBase, metaclass=Singleton):
    """
    推荐处理链，单例运行
    """

    # 推荐缓存时间
    recommend_ttl = 24 * 3600
    # 推荐缓存页数
    cache_max_pages = 5
    # 推荐缓存区域
    recommend_cache_region = "recommend"

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
        if not settings.GLOBAL_IMAGE_CACHE:
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
        movies = await TmdbChain().async_run_module("async_tmdb_discover", mtype=MediaType.MOVIE,
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
        tvs = await TmdbChain().async_run_module("async_tmdb_discover", mtype=MediaType.TV,
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
        infos = await TmdbChain().async_run_module(
            "async_tmdb_trending",
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
        medias = await BangumiChain().async_run_module("async_bangumi_calendar")
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣正在热映
        """
        movies = await DoubanChain().async_run_module("async_movie_showing", page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movies(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                                  page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电影
        """
        movies = await DoubanChain().async_run_module("async_douban_discover", mtype=MediaType.MOVIE,
                                                      sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tvs(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                               page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电视剧
        """
        tvs = await DoubanChain().async_run_module("async_douban_discover", mtype=MediaType.TV,
                                                   sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣电影TOP250
        """
        movies = await DoubanChain().async_run_module("async_movie_top250", page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣国产剧集榜
        """
        tvs = await DoubanChain().async_run_module("async_tv_weekly_chinese", page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣全球剧集榜
        """
        tvs = await DoubanChain().async_run_module("async_tv_weekly_global", page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门动漫
        """
        tvs = await DoubanChain().async_run_module("async_tv_animation", page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电影
        """
        movies = await DoubanChain().async_run_module("async_movie_hot", page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电视剧
        """
        tvs = await DoubanChain().async_run_module("async_tv_hot", page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []
