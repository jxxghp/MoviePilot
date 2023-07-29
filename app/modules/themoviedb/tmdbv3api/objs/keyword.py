from ..tmdb import TMDb


class Keyword(TMDb):
    _urls = {
        "details": "/keyword/%s",
        "movies": "/keyword/%s/movies"
    }

    def details(self, keyword_id):
        """
        Get a keywords details by id.
        :param keyword_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % keyword_id)

    def movies(self, keyword_id):
        """
        Get the movies of a keyword by id.
        :param keyword_id: int
        :return:
        """
        return self._request_obj(self._urls["movies"] % keyword_id, key="results")
