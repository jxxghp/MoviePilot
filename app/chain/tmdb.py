from typing import Optional, List

from app.chain import ChainBase
from app.schemas import MediaType


class TmdbChain(ChainBase):
    """
    TheMovieDB处理链
    """

    def tmdb_discover(self, mtype: MediaType, sort_by: str, with_genres: str,
                      with_original_language: str, page: int = 1) -> Optional[List[dict]]:
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

    def tmdb_trending(self, page: int = 1) -> List[dict]:
        """
        TMDB流行趋势
        :param page: 第几页
        :return: TMDB信息列表
        """
        return self.run_module("tmdb_trending", page=page)
