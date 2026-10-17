"""
TMDB 条目变更接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Change(TMDb):
    """
    提供电影、剧集和人物的变更列表查询。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "movie": "/movie/changes",
        "tv": "/tv/changes",
        "person": "/person/changes"
    }

    def _change_list(self, change_type: str, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        按媒体类型和日期范围查询发生变更的条目标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param change_type: 变更对象类型，movie、tv 或 person
        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return self._request_obj(
            self._urls[change_type],
            params=params,
            key="results"
        )

    async def _async_change_list(self, change_type: str, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        按媒体类型和日期范围查询发生变更的条目标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param change_type: 变更对象类型，movie、tv 或 person
        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if start_date:
            params += "&start_date=%s" % start_date
        if end_date:
            params += "&end_date=%s" % end_date
        return await self._async_request_obj(
            self._urls[change_type],
            params=params,
            key="results"
        )

    def movie_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的电影标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._change_list("movie", start_date=start_date, end_date=end_date, page=page)

    async def async_movie_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的电影标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_change_list("movie", start_date=start_date, end_date=end_date, page=page)

    def tv_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的电视剧标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._change_list("tv", start_date=start_date, end_date=end_date, page=page)

    async def async_tv_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的电视剧标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_change_list("tv", start_date=start_date, end_date=end_date, page=page)

    def person_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的人物标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._change_list("person", start_date=start_date, end_date=end_date, page=page)

    async def async_person_change_list(self, start_date: str | None = "", end_date: str | None = "", page: int | None = 1) -> TMDbJson:
        """
        查询指定日期范围内发生变更的人物标识。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param start_date: 开始日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param end_date: 结束日期，格式 YYYY-MM-DD；空值不发送该筛选参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_change_list("person", start_date=start_date, end_date=end_date, page=page)
