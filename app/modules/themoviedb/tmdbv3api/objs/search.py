"""
TMDB 关键词搜索接口的同步与异步调用封装。
"""

from typing import Any
from urllib.parse import quote

from ..tmdb import TMDb


class Search(TMDb):
    """
    提供电影、剧集、人物及其他实体的同步和异步搜索。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "companies": "/search/company",
        "collections": "/search/collection",
        "keywords": "/search/keyword",
        "movies": "/search/movie",
        "multi": "/search/multi",
        "people": "/search/person",
        "tv_shows": "/search/tv",
    }

    def companies(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索制作公司。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def collections(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索电影合集。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def keywords(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索关键词。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    def movies(self, term: Any, adult: Any=None, region: Any | None=None, year: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        按关键词搜索电影。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param year: 电影年份筛选，按服务端 year 参数语义处理
        :param release_year: 电影主要上映年份，None 时省略
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        if year is not None:
            params += "&year=%s" % year
        if release_year is not None:
            params += "&primary_release_year=%s" % release_year
        return self._request_obj(
            self._urls["movies"],
            params=params,
            key="results"
        )

    def multi(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        按关键词搜索电影、电视剧和人物。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["multi"],
            params=params,
            key="results"
        )

    def people(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        按关键词搜索人物。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return self._request_obj(
            self._urls["people"],
            params=params,
            key="results"
        )

    def tv_shows(self, term: Any, adult: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        按关键词搜索电视剧。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param release_year: 电视剧首次播出年份，None 时省略
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if release_year is not None:
            params += "&first_air_date_year=%s" % release_year
        return self._request_obj(
            self._urls["tv_shows"],
            params=params,
            key="results"
        )

    async def async_companies(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索制作公司。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["companies"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_collections(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索电影合集。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["collections"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_keywords(self, term: Any, page: Any=1) -> Any:
        """
        按关键词搜索关键词。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["keywords"],
            params="query=%s&page=%s" % (quote(term), page),
            key="results"
        )

    async def async_people(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        按关键词搜索人物。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return await self._async_request_obj(
            self._urls["people"],
            params=params,
            key="results"
        )

    async def async_multi(self, term: Any, adult: Any=None, region: Any | None=None, page: Any=1) -> Any:
        """
        按关键词搜索电影、电视剧和人物。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        return await self._async_request_obj(
            self._urls["multi"],
            params=params,
            key="results"
        )

    async def async_movies(self, term: Any, adult: Any=None, region: Any | None=None, year: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        按关键词搜索电影。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息；None 时不发送该参数
        :param year: 电影年份筛选，按服务端 year 参数语义处理
        :param release_year: 电影主要上映年份，None 时省略
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if region is not None:
            params += "&region=%s" % quote(region)
        if year is not None:
            params += "&year=%s" % year
        if release_year is not None:
            params += "&primary_release_year=%s" % release_year

        return await self._async_request_obj(
            self._urls["movies"],
            params=params,
            key="results"
        )

    async def async_tv_shows(self, term: Any, adult: Any=None, release_year: Any=None, page: Any=1) -> Any:
        """
        按关键词搜索电视剧。

        :param term: 待搜索关键词，由本方法进行 URL 编码
        :param adult: 是否包含成人内容；None 时不发送筛选参数
        :param release_year: 电视剧首次播出年份，None 时省略
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "query=%s&page=%s" % (quote(term), page)
        if adult is not None:
            params += "&include_adult=%s" % ("true" if adult else "false")
        if release_year is not None:
            params += "&first_air_date_year=%s" % release_year
        return await self._async_request_obj(
            self._urls["tv_shows"],
            params=params,
            key="results"
        )
