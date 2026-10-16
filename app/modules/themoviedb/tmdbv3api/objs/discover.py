from typing import Any, Callable, cast
from urllib.parse import urlencode

from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting

from ..tmdb import TMDb, TMDbJson

_cached_discover = cast(
    Callable[[Callable[..., Any]], Callable[..., Any]],
    cached(
        maxsize=1,
        ttl=43200,
        empty_ttl=get_runtime_setting("EMPTY_RESULT_CACHE_TTL"),
    ),
)


class Discover(TMDb):
    _urls = {
        "movies": "/discover/movie",
        "tv": "/discover/tv"
    }

    @_cached_discover
    def discover_movies(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        Discover movies by different types of data like average rating, number of votes, genres and certifications.
        :param params_tuple: dict
        :return:
        """
        params = dict(params_tuple)
        return self._request_obj(self._urls["movies"], urlencode(params), key="results", call_cached=False)

    @_cached_discover
    def discover_tv_shows(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        Discover TV shows by different types of data like average rating, number of votes, genres,
        the network they aired on and air dates.
        :param params_tuple: dict
        :return:
        """
        return self._request_obj(self._urls["tv"], urlencode(params_tuple), key="results", call_cached=False)

    @_cached_discover
    async def async_discover_movies(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        Discover movies by different types of data like average rating, number of votes, genres and certifications.（异步版本）
        :param params_tuple: dict
        :return:
        """
        params = dict(params_tuple)
        return await self._async_request_obj(self._urls["movies"], urlencode(params), key="results", call_cached=False)

    @_cached_discover
    async def async_discover_tv_shows(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        Discover TV shows by different types of data like average rating, number of votes, genres,
        the network they aired on and air dates.（异步版本）
        :param params_tuple: dict
        :return:
        """
        return await self._async_request_obj(self._urls["tv"], urlencode(params_tuple), key="results",
                                             call_cached=False)
