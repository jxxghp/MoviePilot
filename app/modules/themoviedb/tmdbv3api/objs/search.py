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

    def companies(self, term: Any, page: Any=1) -> Any:
        """
        Search for companies.
        :param term: Any
        :param page: Any
        :return:
        """
        return self._request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def collections(self, term: Any, page: Any=1) -> Any:
        """
        Search for collections.
        :param term: Any
        :param page: Any
        :return:
        """
        return self._request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def keywords(self, term: Any, page: Any=1) -> Any:
        """
        Search for keywords.
        :param term: Any
        :param page: Any
        :return:
        """
        return self._request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def movies(self, term: Any, adult: Any=None, region: Any | None=None, year: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        Search for movies.
        :param term: Any
        :param adult: bool
        :param region: Any
        :param year: Any
        :param release_year: Any
        :param page: Any
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

    def multi(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        Search multiple models in a single request.
        Multi search currently supports searching for movies, tv shows and people in a single request.
        :param term: Any
        :param adult: bool
        :param region: Any
        :param page: Any
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

    def people(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        Search for people.
        :param term: Any
        :param adult: bool
        :param region: Any
        :param page: Any
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

    def tv_shows(self, term: Any, adult: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        Search for a TV show.
        :param term: Any
        :param adult: bool
        :param release_year: Any
        :param page: Any
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
    async def async_companies(self, term: Any, page: Any=1) -> Any:
        """
        Search for companies.（异步版本）
        :param term: Any
        :param page: Any
        :return:
        """
        return await self._async_request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_collections(self, term: Any, page: Any=1) -> Any:
        """
        Search for collections.（异步版本）
        :param term: Any
        :param page: Any
        :return:
        """
        return await self._async_request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_keywords(self, term: Any, page: Any=1) -> Any:
        """
        Search for keywords.（异步版本）
        :param term: Any
        :param page: Any
        :return:
        """
        return await self._async_request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_people(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        Search for people.（异步版本）
        :param term: Any
        :param adult: bool
        :param region: Any
        :param page: Any
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

    async def async_multi(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        Search multiple models in a single request.（异步版本）
        Multi search currently supports searching for movies, tv shows and people in a single request.
        :param term: Any
        :param adult: bool
        :param region: Any
        :param page: Any
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

    async def async_movies(self, term: Any, adult: Any=None, region: Any | None=None, year: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        Search for movies.（异步版本）
        :param term: Any
        :param adult: bool
        :param region: Any
        :param year: Any
        :param release_year: Any
        :param page: Any
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

    async def async_tv_shows(self, term: Any, adult: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        Search for a TV show.（异步版本）
        :param term: Any
        :param adult: bool
        :param release_year: Any
        :param page: Any
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
