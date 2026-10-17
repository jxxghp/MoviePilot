"""
TMDB 剧集分组接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Group(TMDb):
    """
    按剧集组身份查询分组详情。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/tv/episode_group/%s"
    }

    def details(self, group_id: int | str) -> TMDbJson:
        """
        查询剧集分组详情。

        :param group_id: 目标剧集组的 TMDB 标识
        :return: 响应的 groups 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["details"] % group_id, key="groups")

    async def async_details(self, group_id: int | str) -> TMDbJson:
        """
        查询剧集分组详情。

        :param group_id: 目标剧集组的 TMDB 标识
        :return: 响应的 groups 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["details"] % group_id, key="groups")
