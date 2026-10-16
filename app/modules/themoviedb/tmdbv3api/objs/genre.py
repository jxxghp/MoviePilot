from ..tmdb import TMDb, TMDbJson


class Genre(TMDb):
    """提供电影与剧集分类查询，保留服务端 JSON 字段。"""

    _urls = {
        "movie_list": "/genre/movie/list",
        "tv_list": "/genre/tv/list"
    }

    def movie_list(self) -> TMDbJson:
        """
        Get the list of official genres for movies.
        :return:
        """
        return self._request_obj(self._urls["movie_list"], key="genres")

    def tv_list(self) -> TMDbJson:
        """
        Get the list of official genres for TV shows.
        :return:
        """
        return self._request_obj(self._urls["tv_list"], key="genres")

    # 异步版本方法
    async def async_movie_list(self) -> TMDbJson:
        """
        Get the list of official genres for movies.（异步版本）
        :return:
        """
        return await self._async_request_obj(self._urls["movie_list"], key="genres")

    async def async_tv_list(self) -> TMDbJson:
        """
        Get the list of official genres for TV shows.（异步版本）
        :return:
        """
        return await self._async_request_obj(self._urls["tv_list"], key="genres")
