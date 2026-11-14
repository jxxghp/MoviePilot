"""
TMDB 关键词接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Keyword(TMDb):
    """
    提供关键词详情与关联电影查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/keyword/%s",
        "movies": "/keyword/%s/movies"
    }

    def details(self, keyword_id: int | str) -> TMDbJson:
        """
        查询关键词详情。

        :param keyword_id: 目标关键词的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["details"] % keyword_id)

    def movies(self, keyword_id: int | str) -> TMDbJson:
        """
        查询与该关键词关联的电影。

        :param keyword_id: 目标关键词的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["movies"] % keyword_id, key="results")

    async def async_details(self, keyword_id: int | str) -> TMDbJson:
        """
        查询关键词详情。

        :param keyword_id: 目标关键词的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % keyword_id)

    async def async_movies(self, keyword_id: int | str) -> TMDbJson:
        """
        查询与该关键词关联的电影。

        :param keyword_id: 目标关键词的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["movies"] % keyword_id, key="results")
