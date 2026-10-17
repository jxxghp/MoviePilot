"""
TMDB 条件发现接口的同步与异步调用封装。
"""

from typing import Any, Callable, cast
from urllib.parse import urlencode

from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting

from ..tmdb import TMDb, TMDbJson

_cached_discover = cast(
    Callable[[Callable[..., Any]], Callable[..., Any]],
    cached(
        maxsize=1,
        ttl=43200,
        empty_ttl=get_runtime_setting("EMPTY_RESULT_CACHE_TTL"),
    ),
)


class Discover(TMDb):
    """
    提供 TMDB 条件发现接口。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "movies": "/discover/movie",
        "tv": "/discover/tv"
    }

    @_cached_discover
    def discover_movies(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        按评分、类型、分级等组合条件发现电影。

        结果由方法装饰器缓存 12 小时，空结果使用配置的短时缓存；
        实际发起请求时绕过底层请求缓存，仅返回当前页。

        :param params_tuple: TMDB 发现接口的查询键值对，按 URL 查询参数编码
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = dict(params_tuple)
        return self._request_obj(self._urls["movies"], urlencode(params), key="results", call_cached=False)

    @_cached_discover
    def discover_tv_shows(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        按评分、类型、首播日期等组合条件发现电视剧。

        结果由方法装饰器缓存 12 小时，空结果使用配置的短时缓存；
        实际发起请求时绕过底层请求缓存，仅返回当前页。

        :param params_tuple: TMDB 发现接口的查询键值对，按 URL 查询参数编码
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["tv"], urlencode(params_tuple), key="results", call_cached=False)

    @_cached_discover
    async def async_discover_movies(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        按评分、类型、分级等组合条件发现电影。

        结果由方法装饰器缓存 12 小时，空结果使用配置的短时缓存；
        实际发起请求时绕过底层请求缓存，仅返回当前页。

        :param params_tuple: TMDB 发现接口的查询键值对，按 URL 查询参数编码
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = dict(params_tuple)
        return await self._async_request_obj(self._urls["movies"], urlencode(params), key="results", call_cached=False)

    @_cached_discover
    async def async_discover_tv_shows(
        self,
        params_tuple: tuple[tuple[str, Any], ...],
    ) -> TMDbJson:
        """
        按评分、类型、首播日期等组合条件发现电视剧。

        结果由方法装饰器缓存 12 小时，空结果使用配置的短时缓存；
        实际发起请求时绕过底层请求缓存，仅返回当前页。

        :param params_tuple: TMDB 发现接口的查询键值对，按 URL 查询参数编码
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["tv"], urlencode(params_tuple), key="results",
                                             call_cached=False)
