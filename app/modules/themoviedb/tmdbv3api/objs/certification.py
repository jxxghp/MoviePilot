from ..tmdb import TMDb


class Certification(TMDb):
    _urls = {
        "movie_list": "/certification/movie/list",
        "tv_list": "/certification/tv/list",
    }

    def movie_list(self):
        """
        Get an up to date list of the officially supported movie certifications on TMDB.
        :return:
        """
        return self._request_obj(self._urls["movie_list"], key="certifications")

    def tv_list(self):
        """
        Get an up to date list of the officially supported TV show certifications on TMDB.
        :return:
        """
        return self._request_obj(self._urls["tv_list"], key="certifications")
