"""
TMDB 流媒体服务商接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Provider(TMDb):
    """
    提供按地区筛选的流媒体服务商查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "regions": "/watch/providers/regions",
        "movie": "/watch/providers/movie",
        "tv": "/watch/providers/tv",
    }

    def available_regions(self) -> TMDbJson:
        """
        查询有流媒体可用信息的国家地区。

        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["regions"],
            key="results"
        )

    async def async_available_regions(self) -> TMDbJson:
        """
        查询有流媒体可用信息的国家地区。

        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["regions"],
            key="results"
        )

    def movie_providers(self, region: str | None=None) -> TMDbJson:
        """
        查询可观看电影的流媒体服务商。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["movie"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )

    async def async_movie_providers(self, region: str | None=None) -> TMDbJson:
        """
        查询可观看电影的流媒体服务商。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["movie"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )

    def tv_providers(self, region: str | None=None) -> TMDbJson:
        """
        查询可观看电视剧的流媒体服务商。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["tv"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )

    async def async_tv_providers(self, region: str | None=None) -> TMDbJson:
        """
        查询可观看电视剧的流媒体服务商。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["tv"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )
