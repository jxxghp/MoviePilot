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
