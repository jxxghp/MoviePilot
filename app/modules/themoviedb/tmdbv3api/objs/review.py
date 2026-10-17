"""
TMDB 评论接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Review(TMDb):
    """
    提供 TMDb 评论详情查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/review/%s",
    }

    def details(self, review_id: int | str) -> TMDbJson:
        """
        查询评论详情。

        :param review_id: 目标评论的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["details"] % review_id)

    async def async_details(self, review_id: int | str) -> TMDbJson:
        """
        查询评论详情。

        :param review_id: 目标评论的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % review_id)
