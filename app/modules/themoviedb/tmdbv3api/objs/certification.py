"""
TMDB 内容分级接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Certification(TMDb):
    """
    提供电影与剧集分级查询，保留服务端 JSON 字段。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "movie_list": "/certification/movie/list",
        "tv_list": "/certification/tv/list",
    }

    def movie_list(self) -> TMDbJson:
        """
        查询电影官方内容分级。

        :return: 响应的 certifications 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["movie_list"], key="certifications")

    async def async_movie_list(self) -> TMDbJson:
        """
        查询电影官方内容分级。

        :return: 响应的 certifications 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["movie_list"], key="certifications")

    def tv_list(self) -> TMDbJson:
        """
        查询电视剧官方内容分级。

        :return: 响应的 certifications 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["tv_list"], key="certifications")

    async def async_tv_list(self) -> TMDbJson:
        """
        查询电视剧官方内容分级。

        :return: 响应的 certifications 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["tv_list"], key="certifications")
