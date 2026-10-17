"""
TMDB 趋势榜接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class Trending(TMDb):
    """
    提供 TMDB 趋势接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {"trending": "/trending/%s/%s"}

    def _trending(self, media_type: str="all", time_window: str="day", page: Any=1) -> Any:
        """
        按媒体类型和时间窗口查询趋势榜，绕过请求缓存。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param media_type: 趋势对象类型，all、movie、tv 或 person
        :param time_window: 趋势统计窗口，day 表示日榜，week 表示周榜
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["trending"] % (media_type, time_window),
            params="page=%s" % page,
            key="results",
            call_cached=False
        )

    def all_day(self, page: Any=1) -> Any:
        """
        查询全部媒体与人物日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="all", time_window="day", page=page)

    def all_week(self, page: Any=1) -> Any:
        """
        查询全部媒体与人物周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="all", time_window="week", page=page)

    def movie_day(self, page: Any=1) -> Any:
        """
        查询电影日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="movie", time_window="day", page=page)

    def movie_week(self, page: Any=1) -> Any:
        """
        查询电影周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="movie", time_window="week", page=page)

    def tv_day(self, page: Any=1) -> Any:
        """
        查询电视剧日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="tv", time_window="day", page=page)

    def tv_week(self, page: Any=1) -> Any:
        """
        查询电视剧周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="tv", time_window="week", page=page)

    def person_day(self, page: Any=1) -> Any:
        """
        查询人物日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="person", time_window="day", page=page)

    def person_week(self, page: Any=1) -> Any:
        """
        查询人物周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._trending(media_type="person", time_window="week", page=page)

    async def _async_trending(self, media_type: str="all", time_window: str="day", page: Any=1) -> Any:
        """
        按媒体类型和时间窗口查询趋势榜，绕过请求缓存。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param media_type: 趋势对象类型，all、movie、tv 或 person
        :param time_window: 趋势统计窗口，day 表示日榜，week 表示周榜
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["trending"] % (media_type, time_window),
            params="page=%s" % page,
            key="results",
            call_cached=False
        )

    async def async_all_day(self, page: Any=1) -> Any:
        """
        查询全部媒体与人物日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="all", time_window="day", page=page)

    async def async_all_week(self, page: Any=1) -> Any:
        """
        查询全部媒体与人物周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="all", time_window="week", page=page)

    async def async_movie_day(self, page: Any=1) -> Any:
        """
        查询电影日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="movie", time_window="day", page=page)

    async def async_movie_week(self, page: Any=1) -> Any:
        """
        查询电影周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="movie", time_window="week", page=page)

    async def async_tv_day(self, page: Any=1) -> Any:
        """
        查询电视剧日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="tv", time_window="day", page=page)

    async def async_tv_week(self, page: Any=1) -> Any:
        """
        查询电视剧周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="tv", time_window="week", page=page)

    async def async_person_day(self, page: Any=1) -> Any:
        """
        查询人物日趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="person", time_window="day", page=page)

    async def async_person_week(self, page: Any=1) -> Any:
        """
        查询人物周趋势榜。

        每次调用绕过请求缓存，仅返回当前页结果。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_trending(media_type="person", time_window="week", page=page)
