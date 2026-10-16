from ..tmdb import TMDb, TMDbJson


class Change(TMDb):
    """提供电影、剧集和人物的变更列表查询。"""

    _urls = {
        "movie": "/movie/changes",
        "tv": "/tv/changes",
        "person": "/person/changes"
    }

    def _change_list(self, change_type: str, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """拼装可选时间范围和页码，并原样返回变更列表。"""
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return self._request_obj(
            self._urls[change_type],
            params=params,
            key="results"
        )

    async def _async_change_list(self, change_type: str, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """拼装可选时间范围和页码，并原样返回变更列表。"""
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return await self._async_request_obj(
            self._urls[change_type],
            params=params,
            key="results"
        )

    def movie_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get the changes for a movie. By default only the last 24 hours are returned.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return self._change_list("movie", start_date=start_date, end_date=end_date, page=page)

    async def async_movie_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get the changes for a movie. By default only the last 24 hours are returned.（异步版本）
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return await self._async_change_list("movie", start_date=start_date, end_date=end_date, page=page)

    def tv_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get a list of all of the TV show ids that have been changed in the past 24 hours.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return self._change_list("tv", start_date=start_date, end_date=end_date, page=page)

    async def async_tv_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get a list of all of the TV show ids that have been changed in the past 24 hours.（异步版本）
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return await self._async_change_list("tv", start_date=start_date, end_date=end_date, page=page)

    def person_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get a list of all of the person ids that have been changed in the past 24 hours.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return self._change_list("person", start_date=start_date, end_date=end_date, page=page)

    async def async_person_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        Get a list of all of the person ids that have been changed in the past 24 hours.（异步版本）
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
        """
        return await self._async_change_list("person", start_date=start_date, end_date=end_date, page=page)
