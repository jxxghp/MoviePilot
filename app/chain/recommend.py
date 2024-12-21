from functools import wraps
from typing import Any, Callable

from cachetools import TTLCache
from cachetools.keys import hashkey

from app.chain import ChainBase
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.log import logger
from app.schemas import MediaType
from app.utils.common import log_execution_time
from app.utils.singleton import Singleton

# 推荐相关的专用缓存
recommend_ttl = 6 * 3600
recommend_cache = TTLCache(maxsize=256, ttl=recommend_ttl)


# 推荐缓存装饰器，避免偶发网络获取数据为空时，页面由于缓存为空长时间渲染异常问题
def cached_with_empty_check(func: Callable):
    """
    缓存装饰器，用于缓存函数的返回结果，仅在结果非空时进行缓存

    :param func: 被装饰的函数
    :return: 包装后的函数
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # 使用 cachetools 缓存，构造缓存键
        cache_key = hashkey(*args, **kwargs)
        if cache_key in recommend_cache:
            return recommend_cache[cache_key]
        result = func(*args, **kwargs)
        # 如果返回值为空，则不缓存
        if result in [None, [], {}]:
            return result
        recommend_cache[cache_key] = result
        return result

    return wrapper


class RecommendChain(ChainBase, metaclass=Singleton):
    """
    推荐处理链，单例运行
    """

    def __init__(self):
        super().__init__()
        self.tmdbchain = TmdbChain()
        self.doubanchain = DoubanChain()
        self.bangumichain = BangumiChain()

    def refresh_recommend(self):
        """
        刷新推荐
        """
        self.tmdb_movies()
        self.tmdb_tvs()
        self.tmdb_trending()
        self.bangumi_calendar()
        self.douban_movies()
        self.douban_tvs()
        self.douban_movie_top250()
        self.douban_tv_weekly_chinese()
        self.douban_tv_weekly_global()
        self.douban_tv_animation()
        self.douban_movie_hot()
        self.douban_tv_hot()

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def tmdb_movies(self, sort_by: str = "popularity.desc", with_genres: str = "",
                    with_original_language: str = "", page: int = 1) -> Any:
        """
        TMDB热门电影
        """
        movies = self.tmdbchain.tmdb_discover(mtype=MediaType.MOVIE,
                                              sort_by=sort_by,
                                              with_genres=with_genres,
                                              with_original_language=with_original_language,
                                              page=page)
        return [movie.to_dict() for movie in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def tmdb_tvs(self, sort_by: str = "popularity.desc", with_genres: str = "",
                 with_original_language: str = "", page: int = 1) -> Any:
        """
        TMDB热门电视剧
        """
        tvs = self.tmdbchain.tmdb_discover(mtype=MediaType.TV,
                                           sort_by=sort_by,
                                           with_genres=with_genres,
                                           with_original_language=with_original_language,
                                           page=page)
        return [tv.to_dict() for tv in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def tmdb_trending(self, page: int = 1) -> Any:
        """
        TMDB流行趋势
        """
        infos = self.tmdbchain.tmdb_trending(page=page)
        return [info.to_dict() for info in infos] if infos else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def bangumi_calendar(self, page: int = 1, count: int = 30) -> Any:
        """
        Bangumi每日放送
        """
        medias = self.bangumichain.calendar()
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_movie_showing(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣正在热映
        """
        movies = self.doubanchain.movie_showing(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_movies(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> Any:
        """
        豆瓣最新电影
        """
        movies = self.doubanchain.douban_discover(mtype=MediaType.MOVIE,
                                                  sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_tvs(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> Any:
        """
        豆瓣最新电视剧
        """
        tvs = self.doubanchain.douban_discover(mtype=MediaType.TV,
                                               sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_movie_top250(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣电影TOP250
        """
        movies = self.doubanchain.movie_top250(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_tv_weekly_chinese(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣国产剧集榜
        """
        tvs = self.doubanchain.tv_weekly_chinese(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_tv_weekly_global(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣全球剧集榜
        """
        tvs = self.doubanchain.tv_weekly_global(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_tv_animation(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门动漫
        """
        tvs = self.doubanchain.tv_animation(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_movie_hot(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门电影
        """
        movies = self.doubanchain.movie_hot(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached_with_empty_check
    def douban_tv_hot(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门电视剧
        """
        tvs = self.doubanchain.tv_hot(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []
