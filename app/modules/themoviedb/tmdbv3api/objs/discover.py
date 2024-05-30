from cachetools import TTLCache, cached

from ..tmdb import TMDb

try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode


class Discover(TMDb):
    _urls = {
        "movies": "/discover/movie",
        "tv": "/discover/tv"
    }

    @cached(cache=TTLCache(maxsize=1, ttl=43200))
    def discover_movies(self, params):
        """
        Discover movies by different types of data like average rating, number of votes, genres and certifications.
        :param params: dict
        :return:
        """
        return self._request_obj(self._urls["movies"], urlencode(params), key="results", call_cached=False)

    @cached(cache=TTLCache(maxsize=1, ttl=43200))
    def discover_tv_shows(self, params):
        """
        Discover TV shows by different types of data like average rating, number of votes, genres,
        the network they aired on and air dates.
        :param params: dict
        :return:
        """
        return self._request_obj(self._urls["tv"], urlencode(params), key="results", call_cached=False)
