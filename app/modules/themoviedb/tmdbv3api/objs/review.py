from ..tmdb import TMDb, TMDbJson


class Review(TMDb):
    """提供 TMDb 评论详情查询。"""

    _urls = {
        "details": "/review/%s",
    }

    def details(self, review_id: int | str) -> TMDbJson:
        """
        Get review details by id.
        :param review_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % review_id)

    async def async_details(self, review_id: int | str) -> TMDbJson:
        """
        Get review details by id.（异步版本）
        :param review_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % review_id)
