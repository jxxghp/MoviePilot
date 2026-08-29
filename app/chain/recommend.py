from typing import Any, Callable, List, Optional, Sequence

import pillow_avif  # noqa: F401  # pylint: disable=unused-import  # AVIF 注册副作用

from app.application.image import ImageHelper
from app.chain.bangumi import BangumiChain
from app.chain.base import ChainBase
from app.chain.douban import DoubanChain
from app.chain.listenbrainz import ListenBrainzChain
from app.chain.tmdb import TmdbChain
from app.domain.context import MusicInfo
from app.foundation.singleton import Singleton
from app.runtime.cache import cached, fresh
from app.runtime.execution import log_execution_time
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import normalize_media_source
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)


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

    @staticmethod
    def _music_chart_request(
            range_name: str,
            page: int,
            count: int,
            entity: str,
    ) -> dict[str, Any]:
        """构造同步和异步榜单入口共用的 ListenBrainz 请求。"""
        return {
            "range_name": range_name,
            "page": page,
            "count": count,
            "entity": entity,
        }

    @staticmethod
    def _music_discover_request(
            page: int,
            count: int,
            entity: str,
            mode: str,
            tags: str,
            sort: str,
    ) -> dict[str, Any]:
        """构造同步和异步豆瓣音乐发现入口共用的请求。"""
        return {
            "page": page,
            "count": count,
            "entity": entity,
            "mode": mode,
            "tags": tags,
            "sort": sort,
        }

    @staticmethod
    def _tmdb_discover_request(
            mtype: MediaType,
            sort_by: Optional[str],
            with_genres: Optional[str],
            with_original_language: Optional[str],
            with_keywords: Optional[str],
            with_watch_providers: Optional[str],
            vote_average: Optional[float],
            vote_count: Optional[int],
            release_date: Optional[str],
            page: Optional[int],
    ) -> dict[str, Any]:
        """构造同步和异步 TMDB 发现入口共用的业务请求。"""
        return {
            "mtype": mtype,
            "sort_by": sort_by,
            "with_genres": with_genres,
            "with_original_language": with_original_language,
            "with_keywords": with_keywords,
            "with_watch_providers": with_watch_providers,
            "vote_average": vote_average,
            "vote_count": vote_count,
            "release_date": release_date,
            "page": page,
        }

    @staticmethod
    def _douban_discover_request(
            mtype: MediaType,
            sort: Optional[str],
            tags: Optional[str],
            page: Optional[int],
            count: Optional[int],
    ) -> dict[str, Any]:
        """构造同步和异步豆瓣影视发现入口共用的业务请求。"""
        return {
            "mtype": mtype,
            "sort": sort,
            "tags": tags,
            "page": page,
            "count": count,
        }

    @staticmethod
    def _supports_music_source(media_source: MediaSource) -> bool:
        """判断推荐发现是否支持请求中的音乐数据源。"""
        return normalize_media_source(media_source) == MediaSource.DoubanMusic

    @staticmethod
    def _serialize_medias(
            medias: Optional[Sequence[Any]],
    ) -> list[dict[str, Any]]:
        """把来源媒体对象统一投影为推荐缓存使用的字典。"""
        return [media.to_dict() for media in medias] if medias else []

    @classmethod
    def _serialize_media_page(
            cls,
            medias: Optional[Sequence[Any]],
            page: Optional[int],
            count: Optional[int],
    ) -> list[dict[str, Any]]:
        """按规范化页码截取来源媒体，并统一投影为推荐字典。"""
        if not medias:
            return []
        normalized_page = page or 1
        normalized_count = count or 30
        start = (normalized_page - 1) * normalized_count
        return cls._serialize_medias(medias[start:start + normalized_count])

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
        request = self._music_chart_request(range_name, page, count, entity)
        results = ListenBrainzChain().music_chart(**request)
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
        request = self._music_chart_request(range_name, page, count, entity)
        results = await ListenBrainzChain().async_music_chart(**request)
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
        if not self._supports_music_source(media_source):
            return []
        request = self._music_discover_request(page, count, entity, mode, tags, sort)
        return DoubanChain().music_discover(**request)

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
        if not self._supports_music_source(media_source):
            return []
        request = self._music_discover_request(page, count, entity, mode, tags, sort)
        return await DoubanChain().async_music_discover(**request)

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
                if runtime_stop_state.is_system_stopped:
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
            if runtime_stop_state.is_system_stopped:
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
        request = self._tmdb_discover_request(
            MediaType.MOVIE, sort_by, with_genres, with_original_language,
            with_keywords, with_watch_providers, vote_average, vote_count,
            release_date, page,
        )
        return self._serialize_medias(TmdbChain().tmdb_discover(**request))

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def music_weekly(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """返回 ListenBrainz 本周全站热门音乐。"""
        medias = self.music_chart(
            range_name="this_week",
            page=page or 1,
            count=count or 30,
            entity=MUSIC_ENTITY_ALBUM,
        )
        return self._serialize_medias(medias)

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
        return self._serialize_medias(medias)

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
        request = self._tmdb_discover_request(
            MediaType.TV, sort_by, with_genres, with_original_language,
            with_keywords, with_watch_providers, vote_average, vote_count,
            release_date, page,
        )
        return self._serialize_medias(TmdbChain().tmdb_discover(**request))

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def tmdb_trending(self, page: Optional[int] = 1) -> List[dict]:
        """
        TMDB流行趋势
        """
        return self._serialize_medias(TmdbChain().tmdb_trending(page=page))

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def bangumi_calendar(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        Bangumi每日放送
        """
        return self._serialize_media_page(BangumiChain().calendar(), page, count)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣正在热映
        """
        return self._serialize_medias(
            DoubanChain().movie_showing(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movies(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                      page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣最新电影
        """
        request = self._douban_discover_request(
            MediaType.MOVIE, sort, tags, page, count
        )
        return self._serialize_medias(DoubanChain().douban_discover(**request))

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tvs(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                   page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣最新电视剧
        """
        request = self._douban_discover_request(MediaType.TV, sort, tags, page, count)
        return self._serialize_medias(DoubanChain().douban_discover(**request))

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣电影TOP250
        """
        return self._serialize_medias(
            DoubanChain().movie_top250(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣国产剧集榜
        """
        return self._serialize_medias(
            DoubanChain().tv_weekly_chinese(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣全球剧集榜
        """
        return self._serialize_medias(
            DoubanChain().tv_weekly_global(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门动漫
        """
        return self._serialize_medias(
            DoubanChain().tv_animation(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门电影
        """
        return self._serialize_medias(
            DoubanChain().movie_hot(page=page, count=count)
        )

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    def douban_tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        豆瓣热门电视剧
        """
        return self._serialize_medias(DoubanChain().tv_hot(page=page, count=count))

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
        request = self._tmdb_discover_request(
            MediaType.MOVIE, sort_by, with_genres, with_original_language,
            with_keywords, with_watch_providers, vote_average, vote_count,
            release_date, page,
        )
        movies = await TmdbChain().async_run_module(
            "async_tmdb_discover", **request, raise_exception=raise_exception
        )
        return self._serialize_medias(movies)

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
        request = self._tmdb_discover_request(
            MediaType.TV, sort_by, with_genres, with_original_language,
            with_keywords, with_watch_providers, vote_average, vote_count,
            release_date, page,
        )
        tvs = await TmdbChain().async_run_module(
            "async_tmdb_discover", **request, raise_exception=raise_exception
        )
        return self._serialize_medias(tvs)

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
        return self._serialize_medias(infos)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_bangumi_calendar(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步Bangumi每日放送
        """
        medias = await BangumiChain().async_run_module("async_bangumi_calendar")
        return self._serialize_media_page(medias, page, count)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣正在热映
        """
        movies = await DoubanChain().async_run_module(
            "async_movie_showing", page=page, count=count
        )
        return self._serialize_medias(movies)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_music_weekly(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """异步返回 ListenBrainz 本周全站热门音乐。"""
        medias = await self.async_music_chart(
            range_name="this_week",
            page=page or 1,
            count=count or 30,
            entity=MUSIC_ENTITY_ALBUM,
        )
        return self._serialize_medias(medias)

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
        return self._serialize_medias(medias)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movies(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                                  page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电影
        """
        request = self._douban_discover_request(
            MediaType.MOVIE, sort, tags, page, count
        )
        movies = await DoubanChain().async_run_module(
            "async_douban_discover", **request
        )
        return self._serialize_medias(movies)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tvs(self, sort: Optional[str] = "R", tags: Optional[str] = "",
                               page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣最新电视剧
        """
        request = self._douban_discover_request(MediaType.TV, sort, tags, page, count)
        tvs = await DoubanChain().async_run_module(
            "async_douban_discover", **request
        )
        return self._serialize_medias(tvs)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣电影TOP250
        """
        movies = await DoubanChain().async_run_module(
            "async_movie_top250", page=page, count=count
        )
        return self._serialize_medias(movies)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣国产剧集榜
        """
        tvs = await DoubanChain().async_run_module(
            "async_tv_weekly_chinese", page=page, count=count
        )
        return self._serialize_medias(tvs)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣全球剧集榜
        """
        tvs = await DoubanChain().async_run_module(
            "async_tv_weekly_global", page=page, count=count
        )
        return self._serialize_medias(tvs)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门动漫
        """
        tvs = await DoubanChain().async_run_module(
            "async_tv_animation", page=page, count=count
        )
        return self._serialize_medias(tvs)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电影
        """
        movies = await DoubanChain().async_run_module(
            "async_movie_hot", page=page, count=count
        )
        return self._serialize_medias(movies)

    @log_execution_time(logger=logger)
    @cached(ttl=recommend_ttl, region=recommend_cache_region, skip_empty=True)
    async def async_douban_tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        异步豆瓣热门电视剧
        """
        tvs = await DoubanChain().async_run_module(
            "async_tv_hot", page=page, count=count
        )
        return self._serialize_medias(tvs)
