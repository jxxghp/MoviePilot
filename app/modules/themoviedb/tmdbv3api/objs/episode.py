"""
TMDB 单集接口的同步与异步调用封装。
"""

from typing import Any

from ..tmdb import TMDb


class Episode(TMDb):
    """
    提供 TMDB 剧集接口的同步与异步封装。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/tv/%s/season/%s/episode/%s",
        "account_states": "/tv/%s/season/%s/episode/%s/account_states",
        "changes": "/tv/episode/%s/changes",
        "credits": "/tv/%s/season/%s/episode/%s/credits",
        "external_ids": "/tv/%s/season/%s/episode/%s/external_ids",
        "images": "/tv/%s/season/%s/episode/%s/images",
        "translations": "/tv/%s/season/%s/episode/%s/translations",
        "rate_tv_episode": "/tv/%s/season/%s/episode/%s/rating",
        "delete_rating": "/tv/%s/season/%s/episode/%s/rating",
        "videos": "/tv/%s/season/%s/episode/%s/videos",
    }

    def details(self, tv_id: int, season_num: int, episode_num: int, append_to_response: str | None="trailers,images,casts,translations") -> Any:
        """
        查询单集详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"] % (tv_id, season_num, episode_num),
            params="append_to_response=%s" % append_to_response
        )

    def account_states(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询当前账号对单集的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["account_states"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id
        )

    def changes(self, episode_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定单集的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param episode_id: 目标单集的 TMDB 标识
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
            self._urls["changes"] % episode_id,
            params=params,
            key="changes"
        )

    def credits(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["credits"] % (tv_id, season_num, episode_num))

    def external_ids(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["external_ids"] % (tv_id, season_num, episode_num))

    def images(self, tv_id: int, season_num: int, episode_num: int, include_image_language: str | None=None) -> Any:
        """
        查询单集图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 响应的 stills 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["images"] % (tv_id, season_num, episode_num),
            params="include_image_language=%s" % include_image_language if include_image_language else "",
            key="stills"
        )

    def translations(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["translations"] % (tv_id, season_num, episode_num),
            key="translations"
        )

    def rate_tv_episode(self, tv_id: int, season_num: int, episode_num: int, rating: float) -> Any:
        """
        为当前账号提交单集评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        self._request_obj(
            self._urls["rate_tv_episode"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    def delete_rating(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        删除当前账号的单集评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        """
        self._request_obj(
            self._urls["delete_rating"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    def videos(self, tv_id: int, season_num: int, episode_num: int, include_video_language: str | None=None) -> Any:
        """
        查询单集预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = ""
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return self._request_obj(
            self._urls["videos"] % (tv_id, season_num, episode_num),
            params=params
        )

    async def async_details(self, tv_id: int, season_num: int, episode_num: int,
                            append_to_response: str | None = "trailers,images,casts,translations") -> Any:
        """
        查询单集详情。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param append_to_response: 逗号分隔的附加子接口名称，与详情一并请求
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"] % (tv_id, season_num, episode_num),
            params="append_to_response=%s" % append_to_response
        )

    async def async_account_states(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询当前账号对单集的评分或收藏状态。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["account_states"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id
        )

    async def async_changes(self, episode_id: int, start_date: str | None=None, end_date: str | None=None, page: int=1) -> Any:
        """
        查询指定单集的字段变更记录。

        日期范围交由服务端校验；未提供时使用接口默认时间窗口。

        :param episode_id: 目标单集的 TMDB 标识
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
            self._urls["changes"] % episode_id,
            params=params,
            key="changes"
        )

    async def async_credits(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集演职人员信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["credits"] % (tv_id, season_num, episode_num))

    async def async_external_ids(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集对应的外部平台标识。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["external_ids"] % (tv_id, season_num, episode_num))

    async def async_images(self, tv_id: int, season_num: int, episode_num: int, include_image_language: str | None=None) -> Any:
        """
        查询单集图片信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param include_image_language: 逗号分隔的图片语言代码，可用 null 包含未标注语言的图片；空值省略参数
        :return: 响应的 stills 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["images"] % (tv_id, season_num, episode_num),
            params="include_image_language=%s" % include_image_language if include_image_language else "",
            key="stills"
        )

    async def async_translations(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        查询单集多语言翻译内容。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["translations"] % (tv_id, season_num, episode_num),
            key="translations"
        )

    async def async_rate_tv_episode(self, tv_id: int, season_num: int, episode_num: int, rating: float) -> Any:
        """
        为当前账号提交单集评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param rating: 提交给 TMDB 的评分值，范围与步长由服务端校验
        """
        await self._async_request_obj(
            self._urls["rate_tv_episode"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    async def async_delete_rating(self, tv_id: int, season_num: int, episode_num: int) -> Any:
        """
        删除当前账号的单集评分。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        """
        await self._async_request_obj(
            self._urls["delete_rating"] % (tv_id, season_num, episode_num),
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    async def async_videos(self, tv_id: int, season_num: int, episode_num: int, include_video_language: str | None=None) -> Any:
        """
        查询单集预告片等视频信息。

        :param tv_id: 目标电视剧的 TMDB 标识
        :param season_num: 季号，0 通常表示特别篇
        :param episode_num: 单集在季内的编号
        :param include_video_language: 逗号分隔的视频语言代码；空值省略参数
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        params = ""
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return await self._async_request_obj(
            self._urls["videos"] % (tv_id, season_num, episode_num),
            params=params
        )
