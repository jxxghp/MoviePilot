from ..tmdb import TMDb


class Season(TMDb):
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

    def details(self, tv_id, season_num, append_to_response="videos,trailers,images,credits,translations"):
        """
        Get the TV season details by id.
        :param tv_id: int
        :param season_num: int
        :param append_to_response: str
        :return:
        """
        return self._request_obj(
            self._urls["details"] % (tv_id, season_num),
            params="append_to_response=%s" % append_to_response
        )

    def account_states(self, tv_id, season_num):
        """
        Get all of the user ratings for the season's episodes.
        :param tv_id: int
        :param season_num: int
        :return:
        """
        return self._request_obj(
            self._urls["account_states"] % (tv_id, season_num),
            params="session_id=%s" % self.session_id,
            key="results"
        )

    def aggregate_credits(self, tv_id, season_num):
        """
        Get the aggregate credits for TV season.
        This call differs from the main credits call in that it does not only return the season credits,
        but rather is a view of all the cast & crew for all of the episodes belonging to a season.
        :param tv_id: int
        :param season_num: int
        :return:
        """
        return self._request_obj(self._urls["aggregate_credits"] % (tv_id, season_num))

    def changes(self, season_id, start_date=None, end_date=None, page=1):
        """
        Get the changes for a TV season. By default only the last 24 hours are returned.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param season_id: int
        :param start_date: str
        :param end_date: str
        :param page: int
        :return:
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

    def credits(self, tv_id, season_num):
        """
        Get the credits for TV season.
        :param tv_id: int
        :param season_num: int
        :return:
        """
        return self._request_obj(self._urls["credits"] % (tv_id, season_num))

    def external_ids(self, tv_id, season_num):
        """
        Get the external ids for a TV season.
        :param tv_id: int
        :param season_num: int
        :return:
        """
        return self._request_obj(self._urls["external_ids"] % (tv_id, season_num))

    def images(self, tv_id, season_num, include_image_language=None):
        """
        Get the images that belong to a TV season.
        :param tv_id: int
        :param season_num: int
        :param include_image_language: str
        :return:
        """
        return self._request_obj(
            self._urls["images"] % (tv_id, season_num),
            params="include_image_language=%s" % include_image_language if include_image_language else "",
            key="posters"
        )

    def translations(self, tv_id, season_num):
        """
        Get a list of the translations that exist for a TV show.
        :param tv_id: int
        :param season_num: int
        """
        return self._request_obj(
            self._urls["translations"] % (tv_id, season_num),
            key="translations"
        )

    def videos(self, tv_id, season_num, include_video_language=None, page=1):
        """
        Get the videos that have been added to a TV show.
        :param tv_id: int
        :param season_num: int
        :param include_video_language: str
        :param page: int
        :return:
        """
        params = "page=%s" % page
        if include_video_language:
            params += "&include_video_language=%s" % include_video_language
        return self._request_obj(
            self._urls["videos"] % (tv_id, season_num),
            params=params
        )
