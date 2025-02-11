import io
import tempfile
from pathlib import Path
from typing import List

import pillow_avif  # noqa 用于自动注册AVIF支持
from PIL import Image

from app.chain import ChainBase
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.core.cache import cache_backend, cached
from app.core.config import settings, global_vars
from app.log import logger
from app.schemas import MediaType
from app.utils.common import log_execution_time
from app.utils.http import RequestUtils
from app.utils.security import SecurityUtils
from app.utils.singleton import Singleton

# 推荐相关的专用缓存
recommend_ttl = 24 * 3600
recommend_cache_region = "recommend"


class RecommendChain(ChainBase, metaclass=Singleton):
    """
    推荐处理链，单例运行
    """

    def __init__(self):
        super().__init__()
        self.tmdbchain = TmdbChain()
        self.doubanchain = DoubanChain()
        self.bangumichain = BangumiChain()
        self.cache_max_pages = 5

    def refresh_recommend(self):
        """
        刷新推荐
        """
        logger.debug("Starting to refresh Recommend data.")
        cache_backend.clear(region=recommend_cache_region)
        logger.debug("Recommend Cache has been cleared.")

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
        # 这里避免区间内连续调用相同来源，因此遍历方案为每页遍历所有推荐来源，再进行页数遍历
        for page in range(1, self.cache_max_pages + 1):
            for method in recommend_methods:
                if global_vars.is_system_stopped:
                    return
                if method in methods_finished:
                    continue
                logger.debug(f"Fetch {method.__name__} data for page {page}.")
                data = method(page=page)
                if not data:
                    logger.debug("All recommendation methods have finished fetching data. Ending pagination early.")
                    methods_finished.add(method)
                    continue
                recommends.extend(data)
            # 如果所有方法都已经完成，提前结束循环
            if len(methods_finished) == len(recommend_methods):
                break

        # 缓存收集到的海报
        self.__cache_posters(recommends)
        logger.debug("Recommend data refresh completed.")

    def __cache_posters(self, datas: List[dict]):
        """
        提取 poster_path 并缓存图片
        :param datas: 数据列表
        """
        if not settings.GLOBAL_IMAGE_CACHE:
            return

        for data in datas:
            if global_vars.is_system_stopped:
                return
            poster_path = data.get("poster_path")
            if poster_path:
                poster_url = poster_path.replace("original", "w500")
                logger.debug(f"Caching poster image: {poster_url}")
                self.__fetch_and_save_image(poster_url)

    @staticmethod
    def __fetch_and_save_image(url: str):
        """
        请求并保存图片
        :param url: 图片路径
        """
        if not settings.GLOBAL_IMAGE_CACHE or not url:
            return

        # 生成缓存路径
        sanitized_path = SecurityUtils.sanitize_url_path(url)
        cache_path = settings.CACHE_PATH / "images" / sanitized_path

        # 没有文件类型，则添加后缀，在恶意文件类型和实际需求下的折衷选择
        if not cache_path.suffix:
            cache_path = cache_path.with_suffix(".jpg")

        # 确保缓存路径和文件类型合法
        if not SecurityUtils.is_safe_path(settings.CACHE_PATH, cache_path, settings.SECURITY_IMAGE_SUFFIXES):
            logger.debug(f"Invalid cache path or file type for URL: {url}, sanitized path: {sanitized_path}")
            return

        # 本地存在缓存图片，则直接跳过
        if cache_path.exists():
            logger.debug(f"Cache hit: Image already exists at {cache_path}")
            return

        # 请求远程图片
        referer = "https://movie.douban.com/" if "doubanio.com" in url else None
        proxies = settings.PROXY if not referer else None
        response = RequestUtils(ua=settings.USER_AGENT, proxies=proxies, referer=referer).get_res(url=url)
        if not response:
            logger.debug(f"Empty response for URL: {url}")
            return

        # 验证下载的内容是否为有效图片
        try:
            Image.open(io.BytesIO(response.content)).verify()
        except Exception as e:
            logger.debug(f"Invalid image format for URL {url}: {e}")
            return

        if not cache_path:
            return

        try:
            if not cache_path.parent.exists():
                cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as tmp_file:
                tmp_file.write(response.content)
                temp_path = Path(tmp_file.name)
            temp_path.replace(cache_path)
            logger.debug(f"Successfully cached image at {cache_path} for URL: {url}")
        except Exception as e:
            logger.debug(f"Failed to write cache file {cache_path} for URL {url}: {e}")

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def tmdb_movies(self, sort_by: str = "popularity.desc",
                    with_genres: str = "",
                    with_original_language: str = "",
                    with_keywords: str = "",
                    with_watch_providers: str = "",
                    vote_average: float = 0,
                    vote_count: int = 0,
                    release_date: str = "",
                    page: int = 1) -> List[dict]:
        """
        TMDB热门电影
        """
        movies = self.tmdbchain.tmdb_discover(mtype=MediaType.MOVIE,
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
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def tmdb_tvs(self, sort_by: str = "popularity.desc",
                 with_genres: str = "",
                 with_original_language: str = "zh|en|ja|ko",
                 with_keywords: str = "",
                 with_watch_providers: str = "",
                 vote_average: float = 0,
                 vote_count: int = 0,
                 release_date: str = "",
                 page: int = 1) -> List[dict]:
        """
        TMDB热门电视剧
        """
        tvs = self.tmdbchain.tmdb_discover(mtype=MediaType.TV,
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
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def tmdb_trending(self, page: int = 1) -> List[dict]:
        """
        TMDB流行趋势
        """
        infos = self.tmdbchain.tmdb_trending(page=page)
        return [info.to_dict() for info in infos] if infos else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def bangumi_calendar(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        Bangumi每日放送
        """
        medias = self.bangumichain.calendar()
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]] if medias else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_movie_showing(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣正在热映
        """
        movies = self.doubanchain.movie_showing(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_movies(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣最新电影
        """
        movies = self.doubanchain.douban_discover(mtype=MediaType.MOVIE,
                                                  sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_tvs(self, sort: str = "R", tags: str = "", page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣最新电视剧
        """
        tvs = self.doubanchain.douban_discover(mtype=MediaType.TV,
                                               sort=sort, tags=tags, page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_movie_top250(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣电影TOP250
        """
        movies = self.doubanchain.movie_top250(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣国产剧集榜
        """
        tvs = self.doubanchain.tv_weekly_chinese(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_tv_weekly_global(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣全球剧集榜
        """
        tvs = self.doubanchain.tv_weekly_global(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_tv_animation(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣热门动漫
        """
        tvs = self.doubanchain.tv_animation(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_movie_hot(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣热门电影
        """
        movies = self.doubanchain.movie_hot(page=page, count=count)
        return [media.to_dict() for media in movies] if movies else []

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region)
    def douban_tv_hot(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        豆瓣热门电视剧
        """
        tvs = self.doubanchain.tv_hot(page=page, count=count)
        return [media.to_dict() for media in tvs] if tvs else []
