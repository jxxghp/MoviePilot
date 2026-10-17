"""
TMDB 电视剧接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class TV(TMDb):
    """
    提供 TMDB 电视剧接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/tv/%s",
        "account_states": "/tv/%s/account_states",
        "aggregate_credits": "/tv/%s/aggregate_credits",
        "alternative_titles": "/tv/%s/alternative_titles",
        "changes": "/tv/%s/changes",
        "content_ratings": "/tv/%s/content_ratings",
        "credits": "/tv/%s/credits",
        "episode_groups": "/tv/%s/episode_groups",
        "external_ids": "/tv/%s/external_ids",
        "images": "/tv/%s/images",
        "keywords": "/tv/%s/keywords",
        "recommendations": "/tv/%s/recommendations",
        "reviews": "/tv/%s/reviews",
        "screened_theatrically": "/tv/%s/screened_theatrically",
        "similar": "/tv/%s/similar",
        "translations": "/tv/%s/translations",
        "videos": "/tv/%s/videos",
        "watch_providers": "/tv/%s/watch/providers",
        "rate_tv_show": "/tv/%s/rating",
        "delete_rating": "/tv/%s/rating",
        "latest": "/tv/latest",
        "airing_today": "/tv/airing_today",
        "on_the_air": "/tv/on_the_air",
        "popular": "/tv/popular",
        "top_rated": "/tv/top_rated",
        "group_episodes": "/tv/episode_group/%s",
    }

    def details(self, tv_id: int, append_to_response: str | None="videos,trailers,images,credits,translations") -> Any:
        """
        查询电视剧详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"] % tv_id,
            params="append_to_response=%s" % append_to_response,
        )

    def account_states(self, tv_id: int) -> Any:
        """
        查询当前账号对电视剧的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["account_states"] % tv_id,
            params="session_id=%s" % self.session_id
        )

    def aggregate_credits(self, tv_id: int) -> Any:
        """
        汇总电视剧所含全部单集的演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["aggregate_credits"] % tv_id)

    def alternative_titles(self, tv_id: int) -> Any:
        """
        查询电视剧在不同地区使用的别名。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["alternative_titles"] % tv_id,
            key="results"
        )

    def changes(self, tv_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定电视剧的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param tv_id: 目标电视剧的 TMDB 标识
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
            self._urls["changes"] % tv_id,
            params=params,
            key="changes"
        )

    def content_ratings(self, tv_id: int) -> Any:
        """
        查询电视剧在各地区的内容分级。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["content_ratings"] % tv_id,
            key="results"
        )

    def credits(self, tv_id: int) -> Any:
        """
        查询电视剧演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["credits"] % tv_id)

    def episode_groups(self, tv_id: int) -> Any:
        """
        查询电视剧可用的分集编排组。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["episode_groups"] % tv_id,
            key="results"
        )

    def group_episodes(self, group_id: int | str) -> Any:
        """
        查询指定剧集组中的分组与剧集。

        :param group_id: 目标剧集组的 TMDB 标识
        :return: 响应的 groups 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["group_episodes"] % group_id,
            key="groups"
        )

    def external_ids(self, tv_id: int) -> Any:
        """
        查询电视剧对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["external_ids"] % tv_id)

    def images(self, tv_id: int, include_image_language: str | None=None) -> Any:
        """
        查询电视剧图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["images"] % tv_id,
            params="include_image_language=%s" % include_image_language if include_image_language else ""
        )

    def keywords(self, tv_id: int) -> Any:
        """
        查询电视剧关联关键词。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["keywords"] % tv_id,
            key="results"
        )

    def recommendations(self, tv_id: int, page: int=1) -> Any:
        """
        查询基于该电视剧推荐的其他条目。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["recommendations"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    def reviews(self, tv_id: int, page: int=1) -> Any:
        """
        查询电视剧用户评论。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["reviews"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    def screened_theatrically(self, tv_id: int) -> Any:
        """
        查询曾在影院或电影节放映的季和单集。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["screened_theatrically"] % tv_id,
            key="results"
        )

    def similar(self, tv_id: int, page: int=1) -> Any:
        """
        查询与指定电视剧相似的条目。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["similar"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    def translations(self, tv_id: int) -> Any:
        """
        查询电视剧多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["translations"] % tv_id,
            key="translations"
        )

    def videos(self, tv_id: int, include_video_language: str | None=None, page: int=1) -> Any:
        """
        查询电视剧预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = "page=%s" % page
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return self._request_obj(
            self._urls["videos"] % tv_id,
            params=params
        )

    def watch_providers(self, tv_id: int) -> Any:
        """
        查询电视剧在各地区的流媒体可用信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["watch_providers"] % tv_id,
            key="results"
        )

    def rate_tv_show(self, tv_id: int, rating: float) -> None:
        """
        为当前账号提交电视剧评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        self._request_obj(
            self._urls["rate_tv_show"] % tv_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    def delete_rating(self, tv_id: int) -> None:
        """
        删除当前账号的电视剧评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        """
        self._request_obj(
            self._urls["delete_rating"] % tv_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    def latest(self) -> Any:
        """
        查询最新创建的电视剧条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["latest"])

    def airing_today(self, page: int=1) -> Any:
        """
        查询今日播出的电视剧。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["airing_today"],
            params="page=%s" % page,
            key="results"
        )

    def on_the_air(self, page: int=1) -> Any:
        """
        查询当前正在播出的电视剧。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["on_the_air"],
            params="page=%s" % page,
            key="results"
        )

    def popular(self, page: int=1) -> Any:
        """
        查询当前热门电视剧列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["popular"],
            params="page=%s" % page,
            key="results"
        )

    def top_rated(self, page: int=1) -> Any:
        """
        查询高评分电视剧列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["top_rated"],
            params="page=%s" % page,
            key="results"
        )

    async def async_details(self, tv_id: int, append_to_response: str | None="videos,trailers,images,credits,translations") -> Any:
        """
        查询电视剧详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"] % tv_id,
            params="append_to_response=%s" % append_to_response,
        )

    async def async_account_states(self, tv_id: int) -> Any:
        """
        查询当前账号对电视剧的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["account_states"] % tv_id,
            params="session_id=%s" % self.session_id
        )

    async def async_aggregate_credits(self, tv_id: int) -> Any:
        """
        汇总电视剧所含全部单集的演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["aggregate_credits"] % tv_id)

    async def async_alternative_titles(self, tv_id: int) -> Any:
        """
        查询电视剧在不同地区使用的别名。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["alternative_titles"] % tv_id,
            key="results"
        )

    async def async_changes(self, tv_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定电视剧的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param tv_id: 目标电视剧的 TMDB 标识
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
            self._urls["changes"] % tv_id,
            params=params,
            key="changes"
        )

    async def async_content_ratings(self, tv_id: int) -> Any:
        """
        查询电视剧在各地区的内容分级。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["content_ratings"] % tv_id,
            key="results"
        )

    async def async_credits(self, tv_id: int) -> Any:
        """
        查询电视剧演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["credits"] % tv_id)

    async def async_episode_groups(self, tv_id: int) -> Any:
        """
        查询电视剧可用的分集编排组。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["episode_groups"] % tv_id,
            key="results"
        )

    async def async_group_episodes(self, group_id: int | str) -> Any:
        """
        查询指定剧集组中的分组与剧集。

        :param group_id: 目标剧集组的 TMDB 标识
        :return: 响应的 groups 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["group_episodes"] % group_id,
            key="groups"
        )

    async def async_external_ids(self, tv_id: int) -> Any:
        """
        查询电视剧对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["external_ids"] % tv_id)

    async def async_images(self, tv_id: int, include_image_language: str | None=None) -> Any:
        """
        查询电视剧图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % tv_id,
            params="include_image_language=%s" % include_image_language if include_image_language else ""
        )

    async def async_keywords(self, tv_id: int) -> Any:
        """
        查询电视剧关联关键词。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["keywords"] % tv_id,
            key="results"
        )

    async def async_recommendations(self, tv_id: int, page: int=1) -> Any:
        """
        查询基于该电视剧推荐的其他条目。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["recommendations"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_reviews(self, tv_id: int, page: int=1) -> Any:
        """
        查询电视剧用户评论。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["reviews"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_screened_theatrically(self, tv_id: int) -> Any:
        """
        查询曾在影院或电影节放映的季和单集。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["screened_theatrically"] % tv_id,
            key="results"
        )

    async def async_similar(self, tv_id: int, page: int=1) -> Any:
        """
        查询与指定电视剧相似的条目。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["similar"] % tv_id,
            params="page=%s" % page,
            key="results"
        )

    async def async_translations(self, tv_id: int) -> Any:
        """
        查询电视剧多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["translations"] % tv_id,
            key="translations"
        )

    async def async_videos(self, tv_id: int, include_video_language: str | None=None, page: int=1) -> Any:
        """
        查询电视剧预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = "page=%s" % page
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return await self._async_request_obj(
            self._urls["videos"] % tv_id,
            params=params
        )

    async def async_watch_providers(self, tv_id: int) -> Any:
        """
        查询电视剧在各地区的流媒体可用信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["watch_providers"] % tv_id,
            key="results"
        )

    async def async_rate_tv_show(self, tv_id: int, rating: float) -> None:
        """
        为当前账号提交电视剧评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        await self._async_request_obj(
            self._urls["rate_tv_show"] % tv_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    async def async_delete_rating(self, tv_id: int) -> None:
        """
        删除当前账号的电视剧评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        """
        await self._async_request_obj(
            self._urls["delete_rating"] % tv_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    async def async_latest(self) -> Any:
        """
        查询最新创建的电视剧条目。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["latest"])

    async def async_airing_today(self, page: int=1) -> Any:
        """
        查询今日播出的电视剧。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["airing_today"],
            params="page=%s" % page,
            key="results"
        )

    async def async_on_the_air(self, page: int=1) -> Any:
        """
        查询当前正在播出的电视剧。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["on_the_air"],
            params="page=%s" % page,
            key="results"
        )

    async def async_popular(self, page: int=1) -> Any:
        """
        查询当前热门电视剧列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["popular"],
            params="page=%s" % page,
            key="results"
        )

    async def async_top_rated(self, page: int=1) -> Any:
        """
        查询高评分电视剧列表。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["top_rated"],
            params="page=%s" % page,
            key="results"
        )
