import os

from ..tmdb import TMDb
from ..exceptions import TMDbException


class Account(TMDb):
    _urls = {
        "details": "/account",
        "created_lists": "/account/%s/lists",
        "favorite_movies": "/account/%s/favorite/movies",
        "favorite_tv": "/account/%s/favorite/tv",
        "favorite": "/account/%s/favorite",
        "rated_movies": "/account/%s/rated/movies",
        "rated_tv": "/account/%s/rated/tv",
        "rated_episodes": "/account/%s/rated/tv/episodes",
        "movie_watchlist": "/account/%s/watchlist/movies",
        "tv_watchlist": "/account/%s/watchlist/tv",
        "watchlist": "/account/%s/watchlist",
    }

    @property
    def account_id(self):
        if not os.environ.get("TMDB_ACCOUNT_ID"):
            os.environ["TMDB_ACCOUNT_ID"] = str(self.details()["id"])
        return os.environ.get("TMDB_ACCOUNT_ID")

    def details(self):
        """
        Get your account details.
        :return:
        """
        return self._request_obj(
            self._urls["details"],
            params="session_id=%s" % self.session_id
        )

    def created_lists(self, page=1):
        """
        Get all of the lists created by an account. Will include private lists if you are the owner.
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["created_lists"] % self.account_id,
            params="session_id=%s&page=%s" % (self.session_id, page),
            key="results"
        )

    def _get_list(self, url, asc_sort=True, page=1):
        params = "session_id=%s&page=%s" % (self.session_id, page)
        if asc_sort is False:
            params += "&sort_by=created_at.desc"
        return self._request_obj(
            self._urls[url] % self.account_id,
            params=params,
            key="results"
        )

    def favorite_movies(self, asc_sort=True, page=1):
        """
        Get the list of your favorite movies.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("favorite_movies", asc_sort=asc_sort, page=page)

    def favorite_tv_shows(self, asc_sort=True, page=1):
        """
        Get the list of your favorite TV shows.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("favorite_tv", asc_sort=asc_sort, page=page)

    def mark_as_favorite(self, media_id, media_type, favorite=True):
        """
        This method allows you to mark a movie or TV show as a favorite item.
        :param media_id: int
        :param media_type: str
        :param favorite:bool
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        self._request_obj(
            self._urls["favorite"] % self.account_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "favorite": favorite,
            }
        )

    def unmark_as_favorite(self, media_id, media_type):
        """
        This method allows you to unmark a movie or TV show as a favorite item.
        :param media_id: int
        :param media_type: str
        """
        self.mark_as_favorite(media_id, media_type, favorite=False)

    def rated_movies(self, asc_sort=True, page=1):
        """
        Get a list of all the movies you have rated.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("rated_movies", asc_sort=asc_sort, page=page)

    def rated_tv_shows(self, asc_sort=True, page=1):
        """
        Get a list of all the TV shows you have rated.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("rated_tv", asc_sort=asc_sort, page=page)

    def rated_episodes(self, asc_sort=True, page=1):
        """
        Get a list of all the TV episodes you have rated.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("rated_episodes", asc_sort=asc_sort, page=page)

    def movie_watchlist(self, asc_sort=True, page=1):
        """
        Get a list of all the movies you have added to your watchlist.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("movie_watchlist", asc_sort=asc_sort, page=page)

    def tv_show_watchlist(self, asc_sort=True, page=1):
        """
        Get a list of all the TV shows you have added to your watchlist.
        :param asc_sort: bool
        :param page: int
        :return:
        """
        return self._get_list("tv_watchlist", asc_sort=asc_sort, page=page)

    def add_to_watchlist(self, media_id, media_type, watchlist=True):
        """
        Add a movie or TV show to your watchlist.
        :param media_id: int
        :param media_type: str
        :param watchlist: bool
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        self._request_obj(
            self._urls["watchlist"] % self.account_id,
            "session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "watchlist": watchlist,
            }
        )

    def remove_from_watchlist(self, media_id, media_type):
        """
        Remove a movie or TV show from your watchlist.
        :param media_id: int
        :param media_type: str
        """
        self.add_to_watchlist(media_id, media_type, watchlist=False)
