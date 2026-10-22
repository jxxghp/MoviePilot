"""
TMDB 影视发现、季集、推荐、演职人员与壁纸能力的模块调度入口。
"""

import random
from typing import Any, List, Optional, cast

from app.chain.base import ChainBase
from app.domain.context import MediaInfo
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.tmdb import TmdbEpisode as _SchemaTmdbEpisode
from app.schemas.tmdb import TmdbSeason as _SchemaTmdbSeason
from app.schemas.types import MediaType


class TmdbChain(ChainBase):
    """
    TheMovieDB处理链，单例运行
    """

    def tmdb_discover(self, mtype: MediaType,
                      sort_by: str,
                      with_genres: str,
                      with_original_language: str,
                      with_keywords: str,
                      with_watch_providers: str,
                      vote_average: float,
                      vote_count: int,
                      release_date: str,
                      page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """

        按筛选条件调用 TMDB 发现接口，返回当前页媒体信息。

        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param with_keywords:  关键字
        :param with_watch_providers:  提供商
        :param vote_average:  评分
        :param vote_count:  评分人数
        :param release_date:  上映日期
        :param page:  页码
        :return: 媒体信息列表
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_discover", mtype=mtype,
                               sort_by=sort_by,
                               with_genres=with_genres,
                               with_original_language=with_original_language,
                               with_keywords=with_keywords,
                               with_watch_providers=with_watch_providers,
                               vote_average=vote_average,
                               vote_count=vote_count,
                               release_date=release_date,
                               page=page))

    def tmdb_trending(self, page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """
        TMDB流行趋势

        :param page: 第几页
        :return: TMDB信息列表
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_trending", page=page))

    def tmdb_collection(self, collection_id: int) -> Optional[List[MediaInfo]]:
        """
        根据合集ID查询集合

        :param collection_id:  合集ID
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_collection", collection_id=collection_id))

    def tmdb_seasons(self, tmdbid: int) -> List[_SchemaTmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息

        :param tmdbid: TMDB 媒体标识
        """
        return cast(List[_SchemaTmdbSeason], (self.run_module("tmdb_seasons", tmdbid=tmdbid)) or [])

    def tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息

        :param group_id: 剧集组ID
        """
        return cast(List[_SchemaTmdbSeason], (self.run_module("tmdb_group_seasons", group_id=group_id)) or [])

    def tmdb_episodes(self, tmdbid: int, season: int, episode_group: Optional[str] = None) -> List[_SchemaTmdbEpisode]:
        """
        根据TMDBID查询某季的所有信信息

        :param tmdbid: TMDB 媒体标识
        :param season:  季
        :param episode_group:  剧集组
        """
        return cast(List[_SchemaTmdbEpisode], (self.run_module("tmdb_episodes", tmdbid=tmdbid, season=season, episode_group=episode_group)) or [])

    def movie_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电影

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_movie_similar", tmdbid=tmdbid))

    def tv_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电视剧

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_tv_similar", tmdbid=tmdbid))

    def movie_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电影

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_movie_recommend", tmdbid=tmdbid))

    def tv_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电视剧

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_tv_recommend", tmdbid=tmdbid))

    def movie_credits(self, tmdbid: int, page: Optional[int] = 1) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电影演职人员

        :param tmdbid: TMDB 媒体标识
        :param page:  页码
        """
        return cast(Optional[List[_SchemaMediaPerson]], self.run_module("tmdb_movie_credits", tmdbid=tmdbid, page=page))

    def tv_credits(self, tmdbid: int, page: Optional[int] = 1) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电视剧演职人员

        :param tmdbid: TMDB 媒体标识
        :param page:  页码
        """
        return cast(Optional[List[_SchemaMediaPerson]], self.run_module("tmdb_tv_credits", tmdbid=tmdbid, page=page))

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据TMDBID查询演职员详情

        :param person_id:  人物ID
        """
        return cast(Optional[_SchemaMediaPerson], self.run_module("tmdb_person_detail", person_id=person_id))

    def person_credits(self, person_id: int, page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品

        :param person_id:  人物ID
        :param page:  页码
        """
        return cast(Optional[List[MediaInfo]], self.run_module("tmdb_person_credits", person_id=person_id, page=page))

    def get_random_wallpager(self) -> Optional[str]:
        """
        获取随机壁纸，缓存1个小时
        """
        wallpapers = [info.backdrop_path for info in self.tmdb_trending() or [] if info and info.backdrop_path]
        return random.choice(wallpapers) if wallpapers else None

    def get_trending_wallpapers(self, num: Optional[int] = 10) -> List[str]:
        """
        获取所有流行壁纸
        """
        infos = self.tmdb_trending()
        if infos:
            return [info.backdrop_path for info in infos if info and info.backdrop_path][:num]
        return []

    async def async_tmdb_discover(self, mtype: MediaType,
                                  sort_by: str,
                                  with_genres: str,
                                  with_original_language: str,
                                  with_keywords: str,
                                  with_watch_providers: str,
                                  vote_average: float,
                                  vote_count: int,
                                  release_date: str,
                                  page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """
        发现TMDB电影、剧集（异步版本）

        按筛选条件调用 TMDB 发现接口，返回当前页媒体信息。

        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param with_keywords:  关键字
        :param with_watch_providers:  提供商
        :param vote_average:  评分
        :param vote_count:  评分人数
        :param release_date:  上映日期
        :param page:  页码
        :return: 媒体信息列表
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_discover", mtype=mtype,
                                           sort_by=sort_by,
                                           with_genres=with_genres,
                                           with_original_language=with_original_language,
                                           with_keywords=with_keywords,
                                           with_watch_providers=with_watch_providers,
                                           vote_average=vote_average,
                                           vote_count=vote_count,
                                           release_date=release_date,
                                           page=page))

    async def async_tmdb_trending(self, page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """
        TMDB流行趋势（异步版本）

        :param page: 第几页
        :return: TMDB信息列表
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_trending", page=page))

    async def async_tmdb_collection(self, collection_id: int) -> Optional[List[MediaInfo]]:
        """
        根据合集ID查询集合（异步版本）

        :param collection_id:  合集ID
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_collection", collection_id=collection_id))

    async def async_tmdb_seasons(self, tmdbid: int) -> List[_SchemaTmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息（异步版本）

        :param tmdbid: TMDB 媒体标识
        """
        return cast(List[_SchemaTmdbSeason], (await self.async_run_module("async_tmdb_seasons", tmdbid=tmdbid)) or [])

    async def async_tmdb_group_seasons(self, group_id: str) -> List[_SchemaTmdbSeason]:
        """
        根据剧集组ID查询themoviedb所有季集信息（异步版本）

        :param group_id: 剧集组ID
        """
        return cast(List[_SchemaTmdbSeason], (await self.async_run_module("async_tmdb_group_seasons", group_id=group_id)) or [])

    async def async_tmdb_episodes(self, tmdbid: int, season: int,
                                  episode_group: Optional[str] = None) -> List[_SchemaTmdbEpisode]:
        """
        根据TMDBID查询某季的所有信信息（异步版本）

        :param tmdbid: TMDB 媒体标识
        :param season:  季
        :param episode_group:  剧集组
        """
        return cast(List[_SchemaTmdbEpisode], (await self.async_run_module("async_tmdb_episodes", tmdbid=tmdbid, season=season,
                                           episode_group=episode_group)) or [])

    async def async_movie_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电影（异步版本）

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_movie_similar", tmdbid=tmdbid))

    async def async_tv_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电视剧（异步版本）

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_tv_similar", tmdbid=tmdbid))

    async def async_movie_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电影（异步版本）

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_movie_recommend", tmdbid=tmdbid))

    async def async_tv_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电视剧（异步版本）

        :param tmdbid: TMDB 媒体标识
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_tv_recommend", tmdbid=tmdbid))

    async def async_movie_credits(self, tmdbid: int, page: Optional[int] = 1) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电影演职人员（异步版本）

        :param tmdbid: TMDB 媒体标识
        :param page:  页码
        """
        return cast(Optional[List[_SchemaMediaPerson]], await self.async_run_module("async_tmdb_movie_credits", tmdbid=tmdbid, page=page))

    async def async_tv_credits(self, tmdbid: int, page: Optional[int] = 1) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电视剧演职人员（异步版本）

        :param tmdbid: TMDB 媒体标识
        :param page:  页码
        """
        return cast(Optional[List[_SchemaMediaPerson]], await self.async_run_module("async_tmdb_tv_credits", tmdbid=tmdbid, page=page))

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据TMDBID查询演职员详情（异步版本）

        :param person_id:  人物ID
        """
        return cast(Optional[_SchemaMediaPerson], await self.async_run_module("async_tmdb_person_detail", person_id=person_id))

    async def async_person_credits(self, person_id: int, page: Optional[int] = 1) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品（异步版本）

        :param person_id:  人物ID
        :param page:  页码
        """
        return cast(Optional[List[MediaInfo]], await self.async_run_module("async_tmdb_person_credits", person_id=person_id, page=page))

    async def async_get_random_wallpager(self) -> Optional[str]:
        """
        获取随机壁纸（异步版本），缓存1个小时
        """
        infos = await self.async_tmdb_trending()
        if infos:
            while True:
                info = random.choice(infos)
                if info and info.backdrop_path:
                    return info.backdrop_path
        return None

    async def async_get_trending_wallpapers(self, num: Optional[int] = 10) -> List[str]:
        """
        获取所有流行壁纸（异步版本）
        """
        infos = await self.async_tmdb_trending()
        if infos:
            return [info.backdrop_path for info in infos if info and info.backdrop_path][:num]
        return []

    def cache_items(self) -> list[dict[str, Any]]:
        """
        查询TMDB识别缓存条目列表
        """
        result = self.run_module("tmdb_cache_items")
        return cast(list[dict[str, Any]], result or [])

    def delete_cache(self, cache_key: str) -> dict[str, Any]:
        """
        按缓存键删除单条TMDB识别缓存
        """
        result = self.run_module("tmdb_cache_delete", cache_key=cache_key)
        return cast(dict[str, Any], result or {})

    def clear_cache(self) -> None:
        """
        清空全部TMDB识别缓存
        """
        self.run_module("tmdb_cache_clear")
