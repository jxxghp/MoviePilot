"""
TMDB 制作公司接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Company(TMDb):
    """
    提供公司详情、别名、图像和电影分页查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/company/%s",
        "alternative_names": "/company/%s/alternative_names",
        "images": "/company/%s/images",
        "movies": "/company/%s/movies"
    }

    def details(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司详情。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["details"] % company_id)

    def alternative_names(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司别名。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["alternative_names"] % company_id, key="results")

    def images(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司图片信息。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 响应的 logos 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["images"] % company_id, key="logos")

    def movies(self, company_id: int | str, page: int=1) -> TMDbJson:
        """
        查询与该制作公司关联的电影。

        :param company_id: 目标制作公司的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["movies"] % company_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_details(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司详情。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % company_id)

    async def async_alternative_names(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司别名。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["alternative_names"] % company_id, key="results")

    async def async_images(self, company_id: int | str) -> TMDbJson:
        """
        查询制作公司图片信息。

        :param company_id: 目标制作公司的 TMDB 标识
        :return: 响应的 logos 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["images"] % company_id, key="logos")

    async def async_movies(self, company_id: int | str, page: int=1) -> TMDbJson:
        """
        查询与该制作公司关联的电影。

        :param company_id: 目标制作公司的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["movies"] % company_id,
            params="page=%s" % page,
            key="results"
        )
