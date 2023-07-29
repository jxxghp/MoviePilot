from ..tmdb import TMDb


class Genre(TMDb):
    _urls = {
        "movie_list": "/genre/movie/list",
        "tv_list": "/genre/tv/list"
    }

    def movie_list(self):
        """
        Get the list of official genres for movies.
        :return:
        """
        return self._request_obj(self._urls["movie_list"], key="genres")

    def tv_list(self):
        """
        Get the list of official genres for TV shows.
        :return:
        """
        return self._request_obj(self._urls["tv_list"], key="genres")
