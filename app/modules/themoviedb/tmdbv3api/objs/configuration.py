import warnings

from ..tmdb import TMDb


class Configuration(TMDb):
    _urls = {
        "api_configuration": "/configuration",
        "countries": "/configuration/countries",
        "jobs": "/configuration/jobs",
        "languages": "/configuration/languages",
        "primary_translations": "/configuration/primary_translations",
        "timezones": "/configuration/timezones"
    }

    def info(self):
        warnings.warn("info method is deprecated use tmdbv3api.Configuration().api_configuration()",
                      DeprecationWarning)
        return self.api_configuration()

    def api_configuration(self):
        """
        Get the system wide configuration info.
        """
        return self._request_obj(self._urls["api_configuration"])

    def countries(self):
        """
        Get the list of countries (ISO 3166-1 tags) used throughout TMDb.
        """
        return self._request_obj(self._urls["countries"])

    def jobs(self):
        """
        Get a list of the jobs and departments we use on TMDb.
        """
        return self._request_obj(self._urls["jobs"])

    def languages(self):
        """
        Get the list of languages (ISO 639-1 tags) used throughout TMDb.
        """
        return self._request_obj(self._urls["languages"])

    def primary_translations(self):
        """
        Get a list of the officially supported translations on TMDb.
        """
        return self._request_obj(self._urls["primary_translations"])

    def timezones(self):
        """
        Get the list of timezones used throughout TMDb.
        """
        return self._request_obj(self._urls["timezones"])

    # 异步版本方法
    async def async_api_configuration(self):
        """
        Get the system wide configuration info.（异步版本）
        """
        return await self._async_request_obj(self._urls["api_configuration"])

    async def async_countries(self):
        """
        Get the list of countries (ISO 3166-1 tags) used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["countries"])

    async def async_jobs(self):
        """
        Get a list of the jobs and departments we use on TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["jobs"])

    async def async_languages(self):
        """
        Get the list of languages (ISO 639-1 tags) used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["languages"])

    async def async_primary_translations(self):
        """
        Get a list of the officially supported translations on TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["primary_translations"])

    async def async_timezones(self):
        """
        Get the list of timezones used throughout TMDb.（异步版本）
        """
        return await self._async_request_obj(self._urls["timezones"])
