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

    async def async_details(self, review_id):
        """
        Get the primary person details by id.（异步版本）
        :param review_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % review_id)
