"""
TMDB 剧集季接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class Season(TMDb):
    """
    提供 TMDB 季度接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/tv/%s/season/%s",
        "account_states": "/tv/%s/season/%s/account_states",
        "aggregate_credits": "/tv/%s/season/%s/aggregate_credits",
        "changes": "/tv/season/%s/changes",
        "credits": "/tv/%s/season/%s/credits",
        "external_ids": "/tv/%s/season/%s/external_ids",
        "images": "/tv/%s/season/%s/images",
        "translations": "/tv/%s/season/%s/translations",
        "videos": "/tv/%s/season/%s/videos",
    }

    def details(self, tv_id: int, season_num: int, append_to_response: str | None="videos,trailers,images,credits,translations") -> Any:
        """
        查询剧集季详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"] % (tv_id, season_num),
            params="append_to_response=%s" % append_to_response
        )

    def account_states(self, tv_id: int, season_num: int) -> Any:
        """
        查询当前账号对剧集季的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["account_states"] % (tv_id, season_num),
            params="session_id=%s" % self.session_id,
            key="results"
        )

    def aggregate_credits(self, tv_id: int, season_num: int) -> Any:
        """
        汇总剧集季所含全部单集的演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["aggregate_credits"] % (tv_id, season_num))

    def changes(self, season_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定剧集季的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param season_id: 目标季的 TMDB 标识
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
            self._urls["changes"] % season_id,
            params=params,
            key="changes"
        )

    def credits(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["credits"] % (tv_id, season_num))

    def external_ids(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["external_ids"] % (tv_id, season_num))

    def images(self, tv_id: int, season_num: int, include_image_language: str | None=None) -> Any:
        """
        查询剧集季图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 响应的 posters 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["images"] % (tv_id, season_num),
            params="include_image_language=%s" % include_image_language if include_image_language else "",
            key="posters"
        )

    def translations(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["translations"] % (tv_id, season_num),
            key="translations"
        )

    def videos(self, tv_id: int, season_num: int, include_video_language: str | None=None, page: int=1) -> Any:
        """
        查询剧集季预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = "page=%s" % page
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return self._request_obj(
            self._urls["videos"] % (tv_id, season_num),
            params=params
        )

    async def async_details(self, tv_id: int, season_num: int, append_to_response: str | None="videos,trailers,images,credits,translations") -> Any:
        """
        查询剧集季详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"] % (tv_id, season_num),
            params="append_to_response=%s" % append_to_response
        )

    async def async_account_states(self, tv_id: int, season_num: int) -> Any:
        """
        查询当前账号对剧集季的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["account_states"] % (tv_id, season_num),
            params="session_id=%s" % self.session_id,
            key="results"
        )

    async def async_aggregate_credits(self, tv_id: int, season_num: int) -> Any:
        """
        汇总剧集季所含全部单集的演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["aggregate_credits"] % (tv_id, season_num))

    async def async_changes(self, season_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定剧集季的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param season_id: 目标季的 TMDB 标识
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
            self._urls["changes"] % season_id,
            params=params,
            key="changes"
        )

    async def async_credits(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["credits"] % (tv_id, season_num))

    async def async_external_ids(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["external_ids"] % (tv_id, season_num))

    async def async_images(self, tv_id: int, season_num: int, include_image_language: str | None=None) -> Any:
        """
        查询剧集季图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 响应的 posters 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % (tv_id, season_num),
            params="include_image_language=%s" % include_image_language if include_image_language else "",
            key="posters"
        )

    async def async_translations(self, tv_id: int, season_num: int) -> Any:
        """
        查询剧集季多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["translations"] % (tv_id, season_num),
            key="translations"
        )

    async def async_videos(self, tv_id: int, season_num: int, include_video_language: str | None=None, page: int=1) -> Any:
        """
        查询剧集季预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = "page=%s" % page
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return await self._async_request_obj(
            self._urls["videos"] % (tv_id, season_num),
            params=params
        )
