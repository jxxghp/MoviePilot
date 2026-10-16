from ..tmdb import TMDb, TMDbJson


class Certification(TMDb):
    """提供电影与剧集分级查询，保留服务端 JSON 字段。"""

    _urls = {
        "movie_list": "/certification/movie/list",
        "tv_list": "/certification/tv/list",
    }

    def movie_list(self) -> TMDbJson:
        """
        Get an up to date list of the officially supported movie certifications on TMDB.
        :return:
        """
        return self._request_obj(self._urls["movie_list"], key="certifications")

    async def async_movie_list(self) -> TMDbJson:
        """
        Get an up to date list of the officially supported movie certifications on TMDB.（异步版本）
        :return:
        """
        return await self._async_request_obj(self._urls["movie_list"], key="certifications")

    def tv_list(self) -> TMDbJson:
        """
        Get an up to date list of the officially supported TV show certifications on TMDB.
        :return:
        """
        return self._request_obj(self._urls["tv_list"], key="certifications")

    async def async_tv_list(self) -> TMDbJson:
        """
        Get an up to date list of the officially supported TV show certifications on TMDB.（异步版本）
        :return:
        """
        return await self._async_request_obj(self._urls["tv_list"], key="certifications")
