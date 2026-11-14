"""
TMDB 媒体类型接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Genre(TMDb):
    """
    提供电影与剧集分类查询，保留服务端 JSON 字段。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "movie_list": "/genre/movie/list",
        "tv_list": "/genre/tv/list"
    }

    def movie_list(self) -> TMDbJson:
        """
        查询电影官方类型。

        :return: 响应的 genres 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["movie_list"], key="genres")

    def tv_list(self) -> TMDbJson:
        """
        查询电视剧官方类型。

        :return: 响应的 genres 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["tv_list"], key="genres")

    async def async_movie_list(self) -> TMDbJson:
        """
        查询电影官方类型。

        :return: 响应的 genres 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["movie_list"], key="genres")

    async def async_tv_list(self) -> TMDbJson:
        """
        查询电视剧官方类型。

        :return: 响应的 genres 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["tv_list"], key="genres")
