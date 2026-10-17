"""
TMDB 电影接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class Movie(TMDb):
    """
    提供 TMDB 电影接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/movie/%s",
        "account_states": "/movie/%s/account_states",
        "alternative_titles": "/movie/%s/alternative_titles",
        "changes": "/movie/%s/changes",
        "credits": "/movie/%s/credits",
        "external_ids": "/movie/%s/external_ids",
        "images": "/movie/%s/images",
        "keywords": "/movie/%s/keywords",
        "lists": "/movie/%s/lists",
        "recommendations": "/movie/%s/recommendations",
        "release_dates": "/movie/%s/release_dates",
        "reviews": "/movie/%s/reviews",
        "similar": "/movie/%s/similar",
        "translations": "/movie/%s/translations",
        "videos": "/movie/%s/videos",
        "watch_providers": "/movie/%s/watch/providers",
        "rate_movie": "/movie/%s/rating",
        "delete_rating": "/movie/%s/rating",
        "latest": "/movie/latest",
        "now_playing": "/movie/now_playing",
        "popular": "/movie/popular",
        "top_rated": "/movie/top_rated",
        "upcoming": "/movie/upcoming",
    }

    def details(self, movie_id: int, append_to_response: str | None="videos,trailers,images,casts,translations,keywords,release_dates") -> Any:
        """
        查询电影详情。

        :param movie_id: 目标电影的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"] % movie_id,
            params="append_to_response=%s" % append_to_response
        )

    def account_states(self, movie_id: int) -> Any:
        """
        查询当前账号对电影的评分或收藏状态。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["account_states"] % movie_id,
            params="session_id=%s" % self.session_id
        )

    def alternative_titles(self, movie_id: int, country: str | None=None) -> Any:
        """
        查询电影在不同地区使用的别名。

        :param movie_id: 目标电影的 TMDB 标识
        :param country: ISO 3166-1 国家代码；空值不限制地区
        :return: 响应的 titles 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["alternative_titles"] % movie_id,
            params="country=%s" % country if country else "",
            key="titles"
        )

    def changes(self, movie_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定电影的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param movie_id: 目标电影的 TMDB 标识
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
            self._urls["changes"] % movie_id,
            params=params,
            key="changes"
        )

    def credits(self, movie_id: int) -> Any:
        """
        查询电影演职人员信息。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["credits"] % movie_id)

    def external_ids(self, movie_id: int) -> Any:
        """
        查询电影对应的外部平台标识。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["external_ids"] % movie_id)

    def images(self, movie_id: int, include_image_language: str | None=None) -> Any:
        """
        查询电影图片信息。

        :param movie_id: 目标电影的 TMDB 标识
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["images"] % movie_id,
            params="include_image_language=%s" % include_image_language if include_image_language else ""
        )

    def keywords(self, movie_id: int) -> Any:
        """
        查询电影关联关键词。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 keywords 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["keywords"] % movie_id,
            key="keywords"
        )

    def lists(self, movie_id: int, page: int=1) -> Any:
        """
        查询包含该电影的用户片单。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["lists"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def recommendations(self, movie_id: int, page: int=1) -> Any:
        """
        查询基于该电影推荐的其他条目。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["recommendations"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def release_dates(self, movie_id: int) -> Any:
        """
        查询电影各地区上映日期和内容分级。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["release_dates"] % movie_id,
            key="results"
        )

    def reviews(self, movie_id: int, page: int=1) -> Any:
        """
        查询电影用户评论。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["reviews"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def similar(self, movie_id: int, page: int=1) -> Any:
        """
        查询与指定电影相似的条目。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["similar"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def translations(self, movie_id: int) -> Any:
        """
        查询电影多语言翻译内容。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["translations"] % movie_id,
            key="translations"
        )

    def videos(self, movie_id: int, page: int=1) -> Any:
        """
        查询电影预告片等视频信息。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["videos"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def watch_providers(self, movie_id: int) -> Any:
        """
        查询电影在各地区的流媒体可用信息。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["watch_providers"] % movie_id,
            key="results"
        )

    def rate_movie(self, movie_id: int, rating: float) -> None:
        """
        为当前账号提交电影评分。

        :param movie_id: 目标电影的 TMDB 标识
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        self._request_obj(
            self._urls["rate_movie"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    def delete_rating(self, movie_id: int) -> None:
        """
        删除当前账号的电影评分。

        :param movie_id: 目标电影的 TMDB 标识
        """
        self._request_obj(
            self._urls["delete_rating"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    def latest(self) -> Any:
        """
        查询最新创建的电影条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["latest"])

    def now_playing(self, region: str | None=None, page: int=1) -> Any:
        """
        查询正在院线上映的电影。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["now_playing"],
            params=params,
            key="results"
        )

    def popular(self, region: str | None=None, page: int=1) -> Any:
        """
        查询当前热门电影列表。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["popular"],
            params=params,
            key="results"
        )

    def top_rated(self, region: str | None=None, page: int=1) -> Any:
        """
        查询高评分电影列表。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["top_rated"],
            params=params,
            key="results"
        )

    def upcoming(self, region: str | None=None, page: int=1) -> Any:
        """
        查询即将院线上映的电影。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["upcoming"],
            params=params,
            key="results"
        )

    async def async_details(self, movie_id: int,
                            append_to_response: str | None = "videos,trailers,images,casts,translations,keywords,release_dates") -> Any:
        """
        查询电影详情。

        :param movie_id: 目标电影的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"] % movie_id,
            params="append_to_response=%s" % append_to_response
        )

    async def async_account_states(self, movie_id: int) -> Any:
        """
        查询当前账号对电影的评分或收藏状态。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["account_states"] % movie_id,
            params="session_id=%s" % self.session_id
        )

    async def async_alternative_titles(self, movie_id: int, country: str | None=None) -> Any:
        """
        查询电影在不同地区使用的别名。

        :param movie_id: 目标电影的 TMDB 标识
        :param country: ISO 3166-1 国家代码；空值不限制地区
        :return: 响应的 titles 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["alternative_titles"] % movie_id,
            params="country=%s" % country if country else "",
            key="titles"
        )

    async def async_changes(self, movie_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定电影的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param movie_id: 目标电影的 TMDB 标识
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
            self._urls["changes"] % movie_id,
            params=params,
            key="changes"
        )

    async def async_credits(self, movie_id: int) -> Any:
        """
        查询电影演职人员信息。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["credits"] % movie_id)

    async def async_external_ids(self, movie_id: int) -> Any:
        """
        查询电影对应的外部平台标识。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["external_ids"] % movie_id)

    async def async_images(self, movie_id: int, include_image_language: str | None=None) -> Any:
        """
        查询电影图片信息。

        :param movie_id: 目标电影的 TMDB 标识
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % movie_id,
            params="include_image_language=%s" % include_image_language if include_image_language else ""
        )

    async def async_keywords(self, movie_id: int) -> Any:
        """
        查询电影关联关键词。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 keywords 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["keywords"] % movie_id,
            key="keywords"
        )

    async def async_lists(self, movie_id: int, page: int=1) -> Any:
        """
        查询包含该电影的用户片单。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["lists"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_recommendations(self, movie_id: int, page: int=1) -> Any:
        """
        查询基于该电影推荐的其他条目。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["recommendations"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_release_dates(self, movie_id: int) -> Any:
        """
        查询电影各地区上映日期和内容分级。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["release_dates"] % movie_id,
            key="results"
        )

    async def async_reviews(self, movie_id: int, page: int=1) -> Any:
        """
        查询电影用户评论。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["reviews"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_similar(self, movie_id: int, page: int=1) -> Any:
        """
        查询与指定电影相似的条目。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["similar"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_translations(self, movie_id: int) -> Any:
        """
        查询电影多语言翻译内容。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["translations"] % movie_id,
            key="translations"
        )

    async def async_videos(self, movie_id: int, page: int=1) -> Any:
        """
        查询电影预告片等视频信息。

        :param movie_id: 目标电影的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["videos"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_watch_providers(self, movie_id: int) -> Any:
        """
        查询电影在各地区的流媒体可用信息。

        :param movie_id: 目标电影的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["watch_providers"] % movie_id,
            key="results"
        )

    async def async_rate_movie(self, movie_id: int, rating: float) -> None:
        """
        为当前账号提交电影评分。

        :param movie_id: 目标电影的 TMDB 标识
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        await self._async_request_obj(
            self._urls["rate_movie"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    async def async_delete_rating(self, movie_id: int) -> None:
        """
        删除当前账号的电影评分。

        :param movie_id: 目标电影的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["delete_rating"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    async def async_latest(self) -> Any:
        """
        查询最新创建的电影条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["latest"])

    async def async_now_playing(self, region: str | None=None, page: int=1) -> Any:
        """
        查询正在院线上映的电影。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return await self._async_request_obj(
            self._urls["now_playing"],
            params=params,
            key="results"
        )

    async def async_popular(self, region: str | None=None, page: int=1) -> Any:
        """
        查询当前热门电影列表。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return await self._async_request_obj(
            self._urls["popular"],
            params=params,
            key="results"
        )

    async def async_top_rated(self, region: str | None=None, page: int=1) -> Any:
        """
        查询高评分电影列表。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return await self._async_request_obj(
            self._urls["top_rated"],
            params=params,
            key="results"
        )

    async def async_upcoming(self, region: str | None=None, page: int=1) -> Any:
        """
        查询即将院线上映的电影。

        :param region: 国家地区代码，用于筛选当地上映或服务商可用信息
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return await self._async_request_obj(
            self._urls["upcoming"],
            params=params,
            key="results"
        )
