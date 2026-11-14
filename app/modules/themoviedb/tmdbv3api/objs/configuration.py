"""
TMDB 服务配置接口的同步与异步调用封装。
"""

import warnings

from ..tmdb import TMDb, TMDbJson


class Configuration(TMDb):
    """
    提供 TMDb 系统配置查询及旧接口兼容入口。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "api_configuration": "/configuration",
        "countries": "/configuration/countries",
        "jobs": "/configuration/jobs",
        "languages": "/configuration/languages",
        "primary_translations": "/configuration/primary_translations",
        "timezones": "/configuration/timezones"
    }

    def info(self) -> TMDbJson:
        """
        保留旧配置入口及其弃用告警。
        """
        warnings.warn("info method is deprecated use tmdbv3api.Configuration().api_configuration()",
                      DeprecationWarning)
        return self.api_configuration()

    def api_configuration(self) -> TMDbJson:
        """
        查询 TMDB 图片地址、尺寸等全局配置。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["api_configuration"])

    def countries(self) -> TMDbJson:
        """
        查询 TMDB 使用的 ISO 3166-1 国家地区列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["countries"])

    def jobs(self) -> TMDbJson:
        """
        查询演职人员职位及所属部门。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["jobs"])

    def languages(self) -> TMDbJson:
        """
        查询 TMDB 使用的 ISO 639-1 语言列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["languages"])

    def primary_translations(self) -> TMDbJson:
        """
        查询 TMDB 官方支持的主要翻译语言。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["primary_translations"])

    def timezones(self) -> TMDbJson:
        """
        查询 TMDB 使用的时区列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["timezones"])

    async def async_api_configuration(self) -> TMDbJson:
        """
        查询 TMDB 图片地址、尺寸等全局配置。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["api_configuration"])

    async def async_countries(self) -> TMDbJson:
        """
        查询 TMDB 使用的 ISO 3166-1 国家地区列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["countries"])

    async def async_jobs(self) -> TMDbJson:
        """
        查询演职人员职位及所属部门。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["jobs"])

    async def async_languages(self) -> TMDbJson:
        """
        查询 TMDB 使用的 ISO 639-1 语言列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["languages"])

    async def async_primary_translations(self) -> TMDbJson:
        """
        查询 TMDB 官方支持的主要翻译语言。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["primary_translations"])

    async def async_timezones(self) -> TMDbJson:
        """
        查询 TMDB 使用的时区列表。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["timezones"])
