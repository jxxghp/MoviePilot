import random
from typing import Optional, List

from cachetools import cached, TTLCache

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.core.context import MediaInfo
from app.schemas import MediaType
from app.utils.singleton import Singleton


class TmdbChain(ChainBase, metaclass=Singleton):
    """
    TheMovieDB处理链，单例运行
    """

    def tmdb_discover(self, mtype: MediaType, sort_by: str, with_genres: str,
                      with_original_language: str, page: int = 1) -> Optional[List[MediaInfo]]:
        """
        :param mtype:  媒体类型
        :param sort_by:  排序方式
        :param with_genres:  类型
        :param with_original_language:  语言
        :param page:  页码
        :return: 媒体信息列表
        """
        return self.run_module("tmdb_discover", mtype=mtype,
                               sort_by=sort_by, with_genres=with_genres,
                               with_original_language=with_original_language,
                               page=page)

    def tmdb_trending(self, page: int = 1) -> Optional[List[MediaInfo]]:
        """
        TMDB流行趋势
        :param page: 第几页
        :return: TMDB信息列表
        """
        return self.run_module("tmdb_trending", page=page)

    def tmdb_seasons(self, tmdbid: int) -> List[schemas.TmdbSeason]:
        """
        根据TMDBID查询themoviedb所有季信息
        :param tmdbid:  TMDBID
        """
        return self.run_module("tmdb_seasons", tmdbid=tmdbid)

    def tmdb_episodes(self, tmdbid: int, season: int) -> List[schemas.TmdbEpisode]:
        """
        根据TMDBID查询某季的所有信信息
        :param tmdbid:  TMDBID
        :param season:  季
        """
        return self.run_module("tmdb_episodes", tmdbid=tmdbid, season=season)

    def movie_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电影
        :param tmdbid:  TMDBID
        """
        return self.run_module("tmdb_movie_similar", tmdbid=tmdbid)

    def tv_similar(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询类似电视剧
        :param tmdbid:  TMDBID
        """
        return self.run_module("tmdb_tv_similar", tmdbid=tmdbid)

    def movie_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电影
        :param tmdbid:  TMDBID
        """
        return self.run_module("tmdb_movie_recommend", tmdbid=tmdbid)

    def tv_recommend(self, tmdbid: int) -> Optional[List[MediaInfo]]:
        """
        根据TMDBID查询推荐电视剧
        :param tmdbid:  TMDBID
        """
        return self.run_module("tmdb_tv_recommend", tmdbid=tmdbid)

    def movie_credits(self, tmdbid: int, page: int = 1) -> Optional[List[schemas.MediaPerson]]:
        """
        根据TMDBID查询电影演职人员
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        return self.run_module("tmdb_movie_credits", tmdbid=tmdbid, page=page)

    def tv_credits(self, tmdbid: int, page: int = 1) -> Optional[List[schemas.MediaPerson]]:
        """
        根据TMDBID查询电视剧演职人员
        :param tmdbid:  TMDBID
        :param page:  页码
        """
        return self.run_module("tmdb_tv_credits", tmdbid=tmdbid, page=page)

    def person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        根据TMDBID查询演职员详情
        :param person_id:  人物ID
        """
        return self.run_module("tmdb_person_detail", person_id=person_id)

    def person_credits(self, person_id: int, page: int = 1) -> Optional[List[MediaInfo]]:
        """
        根据人物ID查询人物参演作品
        :param person_id:  人物ID
        :param page:  页码
        """
        return self.run_module("tmdb_person_credits", person_id=person_id, page=page)

    @cached(cache=TTLCache(maxsize=1, ttl=3600))
    def get_random_wallpager(self) -> Optional[str]:
        """
        获取随机壁纸，缓存1个小时
        """
        infos = self.tmdb_trending()
        if infos:
            # 随机一个电影
            while True:
                info = random.choice(infos)
                if info and info.backdrop_path:
                    return info.backdrop_path
        return None

    @cached(cache=TTLCache(maxsize=1, ttl=3600))
    def get_trending_wallpapers(self, num: int = 10) -> Optional[List[str]]:
        """
        获取所有流行壁纸
        """
        infos = self.tmdb_trending()
        if infos:
            return [info.backdrop_path for info in infos if info and info.backdrop_path][:num]
        return None
