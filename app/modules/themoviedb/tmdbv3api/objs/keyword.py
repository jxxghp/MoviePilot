from ..tmdb import TMDb, TMDbJson


class Keyword(TMDb):
    """提供关键词详情与关联电影查询。"""

    _urls = {
        "details": "/keyword/%s",
        "movies": "/keyword/%s/movies"
    }

    def details(self, keyword_id: int | str) -> TMDbJson:
        """
        Get a keywords details by id.
        :param keyword_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % keyword_id)

    def movies(self, keyword_id: int | str) -> TMDbJson:
        """
        Get the movies of a keyword by id.
        :param keyword_id: int
        :return:
        """
        return self._request_obj(self._urls["movies"] % keyword_id, key="results")

    # 异步版本方法
    async def async_details(self, keyword_id: int | str) -> TMDbJson:
        """
        Get a keywords details by id.（异步版本）
        :param keyword_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % keyword_id)

    async def async_movies(self, keyword_id: int | str) -> TMDbJson:
        """
        Get the movies of a keyword by id.（异步版本）
        :param keyword_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["movies"] % keyword_id, key="results")
