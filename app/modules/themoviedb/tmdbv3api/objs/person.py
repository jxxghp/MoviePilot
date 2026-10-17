"""
TMDB 人物接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class Person(TMDb):
    """
    提供 TMDB 人物接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/person/%s",
        "changes": "/person/%s/changes",
        "movie_credits": "/person/%s/movie_credits",
        "tv_credits": "/person/%s/tv_credits",
        "combined_credits": "/person/%s/combined_credits",
        "external_ids": "/person/%s/external_ids",
        "images": "/person/%s/images",
        "tagged_images": "/person/%s/tagged_images",
        "translations": "/person/%s/translations",
        "latest": "/person/latest",
        "popular": "/person/popular",
    }

    def details(self, person_id: int, append_to_response: str | None="videos,images") -> Any:
        """
        查询人物详情。

        :param person_id: 目标人物的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"] % person_id,
            params="append_to_response=%s" % append_to_response
        )

    def changes(self, person_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定人物的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param person_id: 目标人物的 TMDB 标识
        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 changes 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return self._request_obj(
            self._urls["changes"] % person_id,
            params=params,
            key="changes"
        )

    def movie_credits(self, person_id: int) -> Any:
        """
        查询人物参与的电影演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["movie_credits"] % person_id)

    def tv_credits(self, person_id: int) -> Any:
        """
        查询人物参与的电视剧演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["tv_credits"] % person_id)

    def combined_credits(self, person_id: int) -> Any:
        """
        同时查询人物的电影和电视剧演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["combined_credits"] % person_id)

    def external_ids(self, person_id: int) -> Any:
        """
        查询人物对应的外部平台标识。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["external_ids"] % person_id)

    def images(self, person_id: int) -> Any:
        """
        查询人物图片信息。

        :param person_id: 目标人物的 TMDB 标识
        :return: 响应的 profiles 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["images"] % person_id,
            key="profiles"
        )

    def tagged_images(self, person_id: int, page: int=1) -> Any:
        """
        查询标记了该人物的图片。

        :param person_id: 目标人物的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["tagged_images"] % person_id,
            params="page=%s" % page,
            key="results"
        )

    def translations(self, person_id: int) -> Any:
        """
        查询人物多语言翻译内容。

        :param person_id: 目标人物的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["translations"] % person_id,
            key="translations"
        )

    def latest(self) -> Any:
        """
        查询最新创建的人物条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["latest"])

    def popular(self, page: int=1) -> Any:
        """
        查询当前热门人物列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["popular"],
            params="page=%s" % page,
            key="results"
        )

    async def async_details(self, person_id: int, append_to_response: str | None="videos,images") -> Any:
        """
        查询人物详情。

        :param person_id: 目标人物的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"] % person_id,
            params="append_to_response=%s" % append_to_response
        )

    async def async_changes(self, person_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定人物的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param person_id: 目标人物的 TMDB 标识
        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 changes 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return await self._async_request_obj(
            self._urls["changes"] % person_id,
            params=params,
            key="changes"
        )

    async def async_movie_credits(self, person_id: int) -> Any:
        """
        查询人物参与的电影演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["movie_credits"] % person_id)

    async def async_tv_credits(self, person_id: int) -> Any:
        """
        查询人物参与的电视剧演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["tv_credits"] % person_id)

    async def async_combined_credits(self, person_id: int) -> Any:
        """
        同时查询人物的电影和电视剧演职记录。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["combined_credits"] % person_id)

    async def async_external_ids(self, person_id: int) -> Any:
        """
        查询人物对应的外部平台标识。

        :param person_id: 目标人物的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["external_ids"] % person_id)

    async def async_images(self, person_id: int) -> Any:
        """
        查询人物图片信息。

        :param person_id: 目标人物的 TMDB 标识
        :return: 响应的 profiles 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % person_id,
            key="profiles"
        )

    async def async_tagged_images(self, person_id: int, page: int=1) -> Any:
        """
        查询标记了该人物的图片。

        :param person_id: 目标人物的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["tagged_images"] % person_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_translations(self, person_id: int) -> Any:
        """
        查询人物多语言翻译内容。

        :param person_id: 目标人物的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["translations"] % person_id,
            key="translations"
        )

    async def async_latest(self) -> Any:
        """
        查询最新创建的人物条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["latest"])

    async def async_popular(self, page: int=1) -> Any:
        """
        查询当前热门人物列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["popular"],
            params="page=%s" % page,
            key="results"
        )
