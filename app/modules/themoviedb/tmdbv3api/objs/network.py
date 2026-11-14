"""
TMDB 电视网络接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Network(TMDb):
    """
    提供电视网络详情、别名和图像查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/network/%s",
        "alternative_names": "/network/%s/alternative_names",
        "images": "/network/%s/images"
    }

    def details(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络详情。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["details"] % network_id)

    async def async_details(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络详情。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % network_id)

    def alternative_names(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络别名。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["alternative_names"] % network_id,
            key="results"
        )

    async def async_alternative_names(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络别名。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["alternative_names"] % network_id,
            key="results"
        )

    def images(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络图片信息。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 响应的 logos 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["images"] % network_id,
            key="logos"
        )

    async def async_images(self, network_id: int | str) -> TMDbJson:
        """
        查询电视网络图片信息。

        :param network_id: 目标电视网络的 TMDB 标识
        :return: 响应的 logos 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % network_id,
            key="logos"
        )
