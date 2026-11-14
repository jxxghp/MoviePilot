"""
TMDB 演职记录接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Credit(TMDb):
    """
    按身份标识查询电影或剧集演职员详情。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/credit/%s"
    }

    def details(self, credit_id: int | str) -> TMDbJson:
        """
        查询演职记录详情。

        :param credit_id: 目标演职记录的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["details"] % credit_id)

    async def async_details(self, credit_id: int | str) -> TMDbJson:
        """
        查询演职记录详情。

        :param credit_id: 目标演职记录的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % credit_id)
