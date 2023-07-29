from ..tmdb import TMDb


class Provider(TMDb):
    _urls = {
        "regions": "/watch/providers/regions",  # TODO:
        "movie": "/watch/providers/movie",  # TODO:
        "tv": "/watch/providers/tv",  # TODO:
    }

    def available_regions(self):
        """
        Returns a list of all of the countries we have watch provider (OTT/streaming) data for.
        :return:
        """
        return self._request_obj(
            self._urls["regions"],
            key="results"
        )

    def movie_providers(self, region=None):
        """
        Returns a list of the watch provider (OTT/streaming) data we have available for movies.
        :return:
        """
        return self._request_obj(
            self._urls["movie"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )

    def tv_providers(self, region=None):
        """
        Returns a list of the watch provider (OTT/streaming) data we have available for TV series.
        :return:
        """
        return self._request_obj(
            self._urls["tv"],
            params="watch_region=%s" % region if region else "",
            key="results"
        )
