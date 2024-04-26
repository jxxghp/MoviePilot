from ..tmdb import TMDb


class Person(TMDb):
    _urls = {
        "details": "/person/%s",
        "changes": "/person/%s/changes",
        "movie_credits": "/person/%s/movie_credits",
        "tv_credits": "/person/%s/tv_credits",
        "combined_credits": "/person/%s/combined_credits",
        "external_ids": "/person/%s/external_ids",
        "images": "/person/%s/images",
        "tagged_images": "/person/%s/tagged_images",
        "translations": "/person/%s/translations",
        "latest": "/person/latest",
        "popular": "/person/popular",
    }

    def details(self, person_id, append_to_response="videos,images"):
        """
        Get the primary person details by id.
        :param append_to_response: str
        :param person_id: int
        :return:
        """
        return self._request_obj(
            self._urls["details"] % person_id,
            params="append_to_response=%s" % append_to_response
        )

    def changes(self, person_id, start_date=None, end_date=None, page=1):
        """
        Get the changes for a person. By default only the last 24 hours are returned.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param person_id: int
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
            self._urls["changes"] % person_id,
            params=params,
            key="changes"
        )

    def movie_credits(self, person_id):
        """
        Get the movie credits for a person.
        :param person_id: int
        :return:
        """
        return self._request_obj(self._urls["movie_credits"] % person_id)

    def tv_credits(self, person_id):
        """
        Get the TV show credits for a person.
        :param person_id: int
        :return:
        """
        return self._request_obj(self._urls["tv_credits"] % person_id)

    def combined_credits(self, person_id):
        """
        Get the movie and TV credits together in a single response.
        :param person_id: int
        :return:
        """
        return self._request_obj(self._urls["combined_credits"] % person_id)

    def external_ids(self, person_id):
        """
        Get the external ids for a person. We currently support the following external sources.
        IMDB ID, Facebook, Freebase MID, Freebase ID, Instagram, TVRage ID, and Twitter
        :param person_id: int
        :return:
        """
        return self._request_obj(self._urls["external_ids"] % person_id)

    def images(self, person_id):
        """
        Get the images for a person.
        :param person_id: int
        :return:
        """
        return self._request_obj(
            self._urls["images"] % person_id,
            key="profiles"
        )

    def tagged_images(self, person_id, page=1):
        """
        Get the images that this person has been tagged in.
        :param person_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["tagged_images"] % person_id,
            params="page=%s" % page,
            key="results"
        )

    def translations(self, person_id):
        """
        Get a list of translations that have been created for a person.
        :param person_id: int
        :return:
        """
        return self._request_obj(
            self._urls["translations"] % person_id,
            key="translations"
        )

    def latest(self):
        """
        Get the most newly created person. This is a live response and will continuously change.
        :return:
        """
        return self._request_obj(self._urls["latest"])

    def popular(self, page=1):
        """
        Get the list of popular people on TMDb. This list updates daily.
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["popular"],
            params="page=%s" % page,
            key="results"
        )
