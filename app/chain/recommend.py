from typing import Any

from app.chain import ChainBase
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.log import logger
from app.schemas import MediaType
from app.utils.common import log_execution_time
from app.utils.singleton import Singleton


class RecommendChain(ChainBase, metaclass=Singleton):
    """
    推荐处理链，单例运行
    """

    def __init__(self):
        super().__init__()
        self.tmdbchain = TmdbChain()
        self.doubanchain = DoubanChain()
        self.bangumichain = BangumiChain()

    @log_execution_time(logger=logger)
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
    def tmdb_trending(self, page: int = 1) -> Any:
        """
        TMDB流行趋势
        """
        infos = self.tmdbchain.tmdb_trending(page=page)
        return [info.to_dict() for info in infos] if infos else []

    @log_execution_time(logger=logger)
    def bangumi_calendar(self, page: int = 1, count: int = 30) -> Any:
        """
        Bangumi每日放送
        """
        medias = self.bangumichain.calendar()
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    def movie_showing(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣正在热映
        """
        movies = self.doubanchain.movie_showing(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    def douban_movies(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> Any:
        """
        豆瓣最新电影
        """
        movies = self.doubanchain.douban_discover(mtype=MediaType.MOVIE,
                                                  sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    def douban_tvs(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> Any:
        """
        豆瓣最新电视剧
        """
        tvs = self.doubanchain.douban_discover(mtype=MediaType.TV,
                                               sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    def movie_top250(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣电影TOP250
        """
        movies = self.doubanchain.movie_top250(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣国产剧集榜
        """
        tvs = self.doubanchain.tv_weekly_chinese(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    def tv_weekly_global(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣全球剧集榜
        """
        tvs = self.doubanchain.tv_weekly_global(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    def tv_animation(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门动漫
        """
        tvs = self.doubanchain.tv_animation(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    def movie_hot(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门电影
        """
        movies = self.doubanchain.movie_hot(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    def tv_hot(self, page: int = 1, count: int = 30) -> Any:
        """
        豆瓣热门电视剧
        """
        tvs = self.doubanchain.tv_hot(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []
