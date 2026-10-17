"""
TMDB 用户片单接口的同步与异步调用封装。
"""

from ..exceptions import TMDbException
from ..tmdb import TMDb, TMDbJson


class List(TMDb):
    """
    提供同步与异步的 TMDb 片单查询和维护接口。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

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
        查询用户片单详情。

        :param list_id: 目标片单的 TMDB 标识
        :return: 响应的 items 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["details"] % list_id, key="items")

    def check_item_status(self, list_id: int | str, movie_id: int | str) -> TMDbJson:
        """
        检查电影是否已包含在指定片单。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应中 item_present 字段的原值，表示该电影是否已在片单内
        :raises KeyError: 响应对象缺少必需字段
        :raises TMDbException: 响应不是 JSON 对象
        """
        result = self._request_obj(self._urls["check_status"] % list_id, params="movie_id=%s" % movie_id)
        return self._required_field(result, "item_present")

    def create_list(self, name: str, description: str) -> TMDbJson:
        """
        为当前账号创建片单。

        :param name: 新片单名称
        :param description: 新片单说明
        :return: 新片单的 list_id 字段原值
        :raises KeyError: 响应对象缺少必需字段
        :raises TMDbException: 响应不是 JSON 对象
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
        向当前账号片单添加电影。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        """
        self._request_obj(
            self._urls["add_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def remove_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        从当前账号片单移除电影。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        """
        self._request_obj(
            self._urls["remove_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def clear_list(self, list_id: int | str) -> None:
        """
        清空当前账号片单中的全部条目。

        :param list_id: 目标片单的 TMDB 标识
        """
        self._request_obj(
            self._urls["clear_list"] % list_id,
            params="session_id=%s&confirm=true" % self.session_id,
            method="POST"
        )

    def delete_list(self, list_id: int | str) -> None:
        """
        删除当前账号片单。

        :param list_id: 目标片单的 TMDB 标识
        """
        self._request_obj(
            self._urls["delete_list"] % list_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    async def async_details(self, list_id: int | str) -> TMDbJson:
        """
        查询用户片单详情。

        :param list_id: 目标片单的 TMDB 标识
        :return: 响应的 items 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % list_id, key="items")

    async def async_check_item_status(self, list_id: int | str, movie_id: int | str) -> TMDbJson:
        """
        检查电影是否已包含在指定片单。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应中 item_present 字段的原值，表示该电影是否已在片单内
        :raises KeyError: 响应对象缺少必需字段
        :raises TMDbException: 响应不是 JSON 对象
        """
        result = await self._async_request_obj(self._urls["check_status"] % list_id, params="movie_id=%s" % movie_id)
        return self._required_field(result, "item_present")

    async def async_create_list(self, name: str, description: str) -> TMDbJson:
        """
        为当前账号创建片单。

        :param name: 新片单名称
        :param description: 新片单说明
        :return: 新片单的 list_id 字段原值
        :raises KeyError: 响应对象缺少必需字段
        :raises TMDbException: 响应不是 JSON 对象
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
        向当前账号片单添加电影。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["add_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    async def async_remove_movie(self, list_id: int | str, movie_id: int | str) -> None:
        """
        从当前账号片单移除电影。

        :param list_id: 目标片单的 TMDB 标识
        :param movie_id: 目标电影的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["remove_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    async def async_clear_list(self, list_id: int | str) -> None:
        """
        清空当前账号片单中的全部条目。

        :param list_id: 目标片单的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["clear_list"] % list_id,
            params="session_id=%s&confirm=true" % self.session_id,
            method="POST"
        )

    async def async_delete_list(self, list_id: int | str) -> None:
        """
        删除当前账号片单。

        :param list_id: 目标片单的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["delete_list"] % list_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    @staticmethod
    def _required_field(response: TMDbJson, field: str) -> TMDbJson:
        """
        读取响应必需字段，保留字段缺失时的 KeyError。
        """
        if not isinstance(response, dict):
            raise TMDbException("TheMovieDb 返回数据格式异常：期望JSON对象")
        return response[field]
