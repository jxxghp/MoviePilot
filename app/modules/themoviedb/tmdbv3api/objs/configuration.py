import warnings

from ..tmdb import TMDb, TMDbJson


class Configuration(TMDb):
    """提供 TMDb 系统配置查询及旧接口兼容入口。"""

    _urls = {
        "api_configuration": "/configuration",
        "countries": "/configuration/countries",
        "jobs": "/configuration/jobs",
        "languages": "/configuration/languages",
        "primary_translations": "/configuration/primary_translations",
        "timezones": "/configuration/timezones"
    }

    def info(self) -> TMDbJson:
        """保留旧配置入口及其弃用告警。"""
        warnings.warn("info method is deprecated use tmdbv3api.Configuration().api_configuration()",
                      DeprecationWarning)
        return self.api_configuration()

    def api_configuration(self) -> TMDbJson:
        """
        Get the system wide configuration info.
        """
        return self._request_obj(self._urls["api_configuration"])

    def countries(self) -> TMDbJson:
        """
        Get the list of countries (ISO 3166-1 tags) used throughout TMDb.
        """
        return self._request_obj(self._urls["countries"])

    def jobs(self) -> TMDbJson:
        """
        Get a list of the jobs and departments we use on TMDb.
        """
        return self._request_obj(self._urls["jobs"])

    def languages(self) -> TMDbJson:
        """
        Get the list of languages (ISO 639-1 tags) used throughout TMDb.
        """
        return self._request_obj(self._urls["languages"])

    def primary_translations(self) -> TMDbJson:
        """
        Get a list of the officially supported translations on TMDb.
        """
        return self._request_obj(self._urls["primary_translations"])

    def timezones(self) -> TMDbJson:
        """
        Get the list of timezones used throughout TMDb.
        """
        return self._request_obj(self._urls["timezones"])

    # 异步版本方法
    async def async_api_configuration(self) -> TMDbJson:
        """
        Get the system wide configuration info.（异步版本）
        """
        return await self._async_request_obj(self._urls["api_configuration"])

    async def async_countries(self) -> TMDbJson:
        """
        Get the list of countries (ISO 3166-1 tags) used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["countries"])

    async def async_jobs(self) -> TMDbJson:
        """
        Get a list of the jobs and departments we use on TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["jobs"])

    async def async_languages(self) -> TMDbJson:
        """
        Get the list of languages (ISO 639-1 tags) used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["languages"])

    async def async_primary_translations(self) -> TMDbJson:
        """
        Get a list of the officially supported translations on TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["primary_translations"])

    async def async_timezones(self) -> TMDbJson:
        """
        Get the list of timezones used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["timezones"])
