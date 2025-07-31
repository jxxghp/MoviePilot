from ..tmdb import TMDb


class Group(TMDb):
    _urls = {
        "details": "/tv/episode_group/%s"
    }

    def details(self, group_id):
        """
        Get the details of a TV episode group.
        :param group_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % group_id, key="groups")

    async def async_details(self, group_id):
        """
        Get the details of a TV episode group.（异步版本）
        :param group_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % group_id, key="groups")
