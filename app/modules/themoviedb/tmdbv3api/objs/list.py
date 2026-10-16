from ..exceptions import TMDbException
from ..tmdb import TMDb, TMDbJson


class List(TMDb):
    """提供同步与异步的 TMDb 片单查询和维护接口。"""

    _urls = {
        "details": "/list/%s",
        "check_status": "/list/%s/item_status",
        "create": "/list",
        "add_movie": "/list/%s/add_item",
        "remove_movie": "/list/%s/remove_item",
        "clear_list": "/list/%s/clear",
        "delete_list": "/list/%s",
    }

    def details(self, list_id: int | str) -> TMDbJson:
        """
        Get list details by id.
        :param list_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % list_id, key="items")

    def check_item_status(self, list_id: int | str, movie_id: int | str) -> TMDbJson:
        """
        You can use this method to check if a movie has already been added to the list.
        :param list_id: int
        :param movie_id: int
        :return:
        """
        result = self._request_obj(self._urls["check_status"] % list_id, params="movie_id=%s" % movie_id)
        return self._required_field(result, "item_present")

    def create_list(self, name: str, description: str) -> TMDbJson:
        """
        Create a list and return its identifier.
        :param name: str
        :param description: str
        :return:
        """
        result = self._request_obj(
            self._urls["create"],
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "name": name,
                "description": description,
                "language": self.language
            }
        )
        return self._required_field(result, "list_id")

    def add_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        Add a movie to a list.
        :param list_id: int
        :param movie_id: int
        """
        self._request_obj(
            self._urls["add_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def remove_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        Remove a movie from a list.
        :param list_id: int
        :param movie_id: int
        """
        self._request_obj(
            self._urls["remove_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def clear_list(self, list_id: int | str) -> None:
        """
        Clear all of the items from a list.
        :param list_id: int
        """
        self._request_obj(
            self._urls["clear_list"] % list_id,
            params="session_id=%s&confirm=true" % self.session_id,
            method="POST"
        )

    def delete_list(self, list_id: int | str) -> None:
        """
        Delete a list.
        :param list_id: int
        """
        self._request_obj(
            self._urls["delete_list"] % list_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    # 异步版本方法
    async def async_details(self, list_id: int | str) -> TMDbJson:
        """
        Get list details by id.（异步版本）
        :param list_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % list_id, key="items")

    async def async_check_item_status(self, list_id: int | str, movie_id: int | str) -> TMDbJson:
        """
        You can use this method to check if a movie has already been added to the list.（异步版本）
        :param list_id: int
        :param movie_id: int
        :return:
        """
        result = await self._async_request_obj(self._urls["check_status"] % list_id, params="movie_id=%s" % movie_id)
        return self._required_field(result, "item_present")

    async def async_create_list(self, name: str, description: str) -> TMDbJson:
        """
        Create a list and return its identifier.（异步版本）
        :param name: str
        :param description: str
        :return:
        """
        result = await self._async_request_obj(
            self._urls["create"],
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "name": name,
                "description": description,
                "language": self.language
            }
        )
        return self._required_field(result, "list_id")

    async def async_add_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        Add a movie to a list.（异步版本）
        :param list_id: int
        :param movie_id: int
        """
        await self._async_request_obj(
            self._urls["add_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    async def async_remove_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        Remove a movie from a list.（异步版本）
        :param list_id: int
        :param movie_id: int
        """
        await self._async_request_obj(
            self._urls["remove_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    async def async_clear_list(self, list_id: int | str) -> None:
        """
        Clear all of the items from a list.（异步版本）
        :param list_id: int
        """
        await self._async_request_obj(
            self._urls["clear_list"] % list_id,
            params="session_id=%s&confirm=true" % self.session_id,
            method="POST"
        )

    async def async_delete_list(self, list_id: int | str) -> None:
        """
        Delete a list.（异步版本）
        :param list_id: int
        """
        await self._async_request_obj(
            self._urls["delete_list"] % list_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    @staticmethod
    def _required_field(response: TMDbJson, field: str) -> TMDbJson:
        """读取响应必需字段，保留字段缺失时的 KeyError。"""
        if not isinstance(response, dict):
            raise TMDbException("TheMovieDb 返回数据格式异常：期望JSON对象")
        return response[field]
