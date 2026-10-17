from typing import Any

from urllib.parse import quote

from ..tmdb import TMDb


class Search(TMDb):
    """提供 TMDB 搜索接口的同步与异步封装。"""
    """提供电影、剧集、人物及其他实体的同步和异步搜索。"""

    _urls = {
        "companies": "/search/company",
        "collections": "/search/collection",
        "keywords": "/search/keyword",
        "movies": "/search/movie",
        "multi": "/search/multi",
        "people": "/search/person",
        "tv_shows": "/search/tv",
    }

    def companies(self, term: str, page: int=1) -> Any:
        """
        Search for companies.
        :param term: str
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def collections(self, term: str, page: int=1) -> Any:
        """
        Search for collections.
        :param term: str
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def keywords(self, term: str, page: int=1) -> Any:
        """
        Search for keywords.
        :param term: str
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def movies(self, term: str, adult: bool | None=None, region: str | None=None, year: int | None=None, release_year: int | None=None, page: int=1) -> Any:
        """
        Search for movies.
        :param term: str
        :param adult: bool
        :param region: str
        :param year: int
        :param release_year: int
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        if year is not None:
            params += "&year=%s" % year
        if release_year is not None:
            params += "&primary_release_year=%s" % release_year
        return self._request_obj(
            self._urls["movies"],
            params=params,
            key="results"
        )

    def multi(self, term: str, adult: bool | None=None, region: str | None=None, page: int=1) -> Any:
        """
        Search multiple models in a single request.
        Multi search currently supports searching for movies, tv shows and people in a single request.
        :param term: str
        :param adult: bool
        :param region: str
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["multi"],
            params=params,
            key="results"
        )

    def people(self, term: str, adult: bool | None=None, region: str | None=None, page: int=1) -> Any:
        """
        Search for people.
        :param term: str
        :param adult: bool
        :param region: str
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["people"],
            params=params,
            key="results"
        )

    def tv_shows(self, term: str, adult: bool | None=None, release_year: int | None=None, page: int=1) -> Any:
        """
        Search for a TV show.
        :param term: str
        :param adult: bool
        :param release_year: int
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if release_year is not None:
            params += "&first_air_date_year=%s" % release_year
        return self._request_obj(
            self._urls["tv_shows"],
            params=params,
            key="results"
        )

    # 异步版本方法
    async def async_companies(self, term: str, page: int=1) -> Any:
        """
        Search for companies.（异步版本）
        :param term: str
        :param page: int
        :return:
        """
        return await self._async_request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_collections(self, term: str, page: int=1) -> Any:
        """
        Search for collections.（异步版本）
        :param term: str
        :param page: int
        :return:
        """
        return await self._async_request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_keywords(self, term: str, page: int=1) -> Any:
        """
        Search for keywords.（异步版本）
        :param term: str
        :param page: int
        :return:
        """
        return await self._async_request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_people(self, term: str, adult: bool | None=None, region: str | None=None, page: int=1) -> Any:
        """
        Search for people.（异步版本）
        :param term: str
        :param adult: bool
        :param region: str
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return await self._async_request_obj(
            self._urls["people"],
            params=params,
            key="results"
        )

    async def async_multi(self, term: str, adult: bool | None=None, region: str | None=None, page: int=1) -> Any:
        """
        Search multiple models in a single request.（异步版本）
        Multi search currently supports searching for movies, tv shows and people in a single request.
        :param term: str
        :param adult: bool
        :param region: str
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return await self._async_request_obj(
            self._urls["multi"],
            params=params,
            key="results"
        )

    async def async_movies(self, term: str, adult: bool | None=None, region: str | None=None, year: int | None=None, release_year: int | None=None, page: int=1) -> Any:
        """
        Search for movies.（异步版本）
        :param term: str
        :param adult: bool
        :param region: str
        :param year: int
        :param release_year: int
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        if year is not None:
            params += "&year=%s" % year
        if release_year is not None:
            params += "&primary_release_year=%s" % release_year

        return await self._async_request_obj(
            self._urls["movies"],
            params=params,
            key="results"
        )

    async def async_tv_shows(self, term: str, adult: bool | None=None, release_year: int | None=None, page: int=1) -> Any:
        """
        Search for a TV show.（异步版本）
        :param term: str
        :param adult: bool
        :param release_year: int
        :param page: int
        :return:
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if release_year is not None:
            params += "&first_air_date_year=%s" % release_year
        return await self._async_request_obj(
            self._urls["tv_shows"],
            params=params,
            key="results"
        )
