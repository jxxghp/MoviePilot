from typing import List

from app.chain import ChainBase
from app.core.context import MediaInfo
from app.log import logger
from app.schemas import MediaType


class TmdbChain(ChainBase):

    def tmdb_movies(self, sort_by: str, with_genres: str, with_original_language: str,
                    page: int = 1) -> List[MediaInfo]:
        """
        浏览TMDB电影信息
        """
        logger.info(f'开始获取TMDB电影列表，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
        movies = self.tmdb_discover(mtype=MediaType.MOVIE,
                                    sort_by=sort_by,
                                    with_genres=with_genres,
                                    with_original_language=with_original_language,
                                    page=page)
        if not movies:
            logger.warn(f'TMDB电影列表为空，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
            return []
        return [MediaInfo(tmdb_info=movie) for movie in movies]

    def tmdb_tvs(self, sort_by: str, with_genres: str, with_original_language: str,
                 page: int = 1) -> List[MediaInfo]:
        """
        浏览TMDB剧集信息
        """
        logger.info(f'开始获取TMDB剧集列表，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
        tvs = self.tmdb_discover(mtype=MediaType.TV,
                                 sort_by=sort_by,
                                 with_genres=with_genres,
                                 with_original_language=with_original_language,
                                 page=page)
        if not tvs:
            logger.warn(f'TMDB剧集列表为空，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
            return []
        return [MediaInfo(tmdb_info=tv) for tv in tvs]
