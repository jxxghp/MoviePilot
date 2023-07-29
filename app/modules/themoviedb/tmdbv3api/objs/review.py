from ..tmdb import TMDb


class Review(TMDb):
    _urls = {
        "details": "/review/%s",
    }

    def details(self, review_id):
        """
        Get the primary person details by id.
        :param review_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % review_id)
