from ..tmdb import TMDb

try:
    from urllib import quote
except ImportError:
    from urllib.parse import quote


class Search(TMDb):
    _urls = {
        "companies": "/search/company",
        "collections": "/search/collection",
        "keywords": "/search/keyword",
        "movies": "/search/movie",
        "multi": "/search/multi",
        "people": "/search/person",
        "tv_shows": "/search/tv",
    }

    def companies(self, term, page=1):
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

    def collections(self, term, page=1):
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

    def keywords(self, term, page=1):
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
    
    def movies(self, term, adult=None, region=None, year=None, release_year=None, page=1):
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
            params += "&include_adult=%s" % "true" if adult else "false"
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

    def multi(self, term, adult=None, region=None, page=1):
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
            params += "&include_adult=%s" % "true" if adult else "false"
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["multi"],
            params=params,
            key="results"
        )

    def people(self, term, adult=None, region=None, page=1):
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
            params += "&include_adult=%s" % "true" if adult else "false"
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["people"],
            params=params,
            key="results"
        )

    def tv_shows(self, term, adult=None, release_year=None, page=1):
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
            params += "&include_adult=%s" % "true" if adult else "false"
        if release_year is not None:
            params += "&first_air_date_year=%s" % release_year
        return self._request_obj(
            self._urls["tv_shows"],
            params=params,
            key="results"
        )
