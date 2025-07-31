from ..tmdb import TMDb


class Credit(TMDb):
    _urls = {
        "details": "/credit/%s"
    }

    def details(self, credit_id):
        """
        Get a movie or TV credit details by id.
        :param credit_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % credit_id)

    async def async_details(self, credit_id):
        """
        Get a movie or TV credit details by id.（异步版本）
        :param credit_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % credit_id)
