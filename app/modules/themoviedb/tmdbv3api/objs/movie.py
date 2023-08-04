from ..tmdb import TMDb


class Movie(TMDb):
    _urls = {
        "details": "/movie/%s",
        "account_states": "/movie/%s/account_states",
        "alternative_titles": "/movie/%s/alternative_titles",
        "changes": "/movie/%s/changes",
        "credits": "/movie/%s/credits",
        "external_ids": "/movie/%s/external_ids",
        "images": "/movie/%s/images",
        "keywords": "/movie/%s/keywords",
        "lists": "/movie/%s/lists",
        "recommendations": "/movie/%s/recommendations",
        "release_dates": "/movie/%s/release_dates",
        "reviews": "/movie/%s/reviews",
        "similar": "/movie/%s/similar",
        "translations": "/movie/%s/translations",
        "videos": "/movie/%s/videos",
        "watch_providers": "/movie/%s/watch/providers",
        "rate_movie": "/movie/%s/rating",
        "delete_rating": "/movie/%s/rating",
        "latest": "/movie/latest",
        "now_playing": "/movie/now_playing",
        "popular": "/movie/popular",
        "top_rated": "/movie/top_rated",
        "upcoming": "/movie/upcoming",
    }

    def details(self, movie_id, append_to_response="videos,trailers,images,casts,translations,keywords,release_dates"):
        """
        Get the primary information about a movie.
        :param movie_id: int
        :param append_to_response: str
        :return:
        """
        return self._request_obj(
            self._urls["details"] % movie_id,
            params="append_to_response=%s" % append_to_response
        )

    def account_states(self, movie_id):
        """
        Grab the following account states for a session:
        Movie rating, If it belongs to your watchlist, or If it belongs to your favourite list.
        :param movie_id: int
        :return:
        """
        return self._request_obj(
            self._urls["account_states"] % movie_id,
            params="session_id=%s" % self.session_id
        )

    def alternative_titles(self, movie_id, country=None):
        """
        Get all of the alternative titles for a movie.
        :param movie_id: int
        :param country: str
        :return:
        """
        return self._request_obj(
            self._urls["alternative_titles"] % movie_id,
            params="country=%s" % country if country else "",
            key="titles"
        )

    def changes(self, movie_id, start_date=None, end_date=None, page=1):
        """
        Get the changes for a movie. By default only the last 24 hours are returned.
        You can query up to 14 days in a single query by using the start_date and end_date query parameters.
        :param movie_id: int
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
            self._urls["changes"] % movie_id,
            params=params,
            key="changes"
        )

    def credits(self, movie_id):
        """
        Get the cast and crew for a movie.
        :param movie_id: int
        :return:
        """
        return self._request_obj(self._urls["credits"] % movie_id)

    def external_ids(self, movie_id):
        """
        Get the external ids for a movie.
        :param movie_id: int
        :return:
        """
        return self._request_obj(self._urls["external_ids"] % movie_id)
    
    def images(self, movie_id, include_image_language=None):
        """
        Get the images that belong to a movie.
        Querying images with a language parameter will filter the results. 
        If you want to include a fallback language (especially useful for backdrops)
        you can use the include_image_language parameter.
        This should be a comma separated value like so: include_image_language=en,null.
        :param movie_id: int
        :param include_image_language: str
        :return:
        """
        return self._request_obj(
            self._urls["images"] % movie_id,
            params="include_image_language=%s" % include_image_language if include_image_language else ""
        )

    def keywords(self, movie_id):
        """
        Get the keywords associated to a movie.
        :param movie_id: int
        :return:
        """
        return self._request_obj(
            self._urls["keywords"] % movie_id,
            key="keywords"
        )

    def lists(self, movie_id, page=1):
        """
        Get a list of lists that this movie belongs to.
        :param movie_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["lists"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def recommendations(self, movie_id, page=1):
        """
        Get a list of recommended movies for a movie.
        :param movie_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["recommendations"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def release_dates(self, movie_id):
        """
        Get the release date along with the certification for a movie.
        :param movie_id: int
        :return:
        """
        return self._request_obj(
            self._urls["release_dates"] % movie_id,
            key="results"
        )

    def reviews(self, movie_id, page=1):
        """
        Get the user reviews for a movie.
        :param movie_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["reviews"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def similar(self, movie_id, page=1):
        """
        Get a list of similar movies.
        :param movie_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["similar"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def translations(self, movie_id):
        """
        Get a list of translations that have been created for a movie.
        :param movie_id: int
        :return:
        """
        return self._request_obj(
            self._urls["translations"] % movie_id,
            key="translations"
        )

    def videos(self, movie_id, page=1):
        """
        Get the videos that have been added to a movie.
        :param movie_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["videos"] % movie_id,
            params="page=%s" % page,
            key="results"
        )

    def watch_providers(self, movie_id):
        """
        You can query this method to get a list of the availabilities per country by provider.
        :param movie_id: int
        :return:
        """
        return self._request_obj(
            self._urls["watch_providers"] % movie_id,
            key="results"
        )

    def rate_movie(self, movie_id, rating):
        """
        Rate a movie.
        :param movie_id: int
        :param rating: float
        """
        self._request_obj(
            self._urls["rate_movie"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"value": rating}
        )

    def delete_rating(self, movie_id):
        """
        Remove your rating for a movie.
        :param movie_id: int
        """
        self._request_obj(
            self._urls["delete_rating"] % movie_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )

    def latest(self):
        """
        Get the most newly created movie. This is a live response and will continuously change.
        :return:
        """
        return self._request_obj(self._urls["latest"])

    def now_playing(self, region=None, page=1):
        """
        Get a list of movies in theatres.
        :param region: str
        :param page: int
        :return:
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["now_playing"],
            params=params,
            key="results"
        )

    def popular(self, region=None, page=1):
        """
        Get a list of the current popular movies on TMDb. This list updates daily.
        :param region: str
        :param page: int
        :return:
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["popular"],
            params=params,
            key="results"
        )

    def top_rated(self, region=None, page=1):
        """
        Get the top rated movies on TMDb.
        :param region: str
        :param page: int
        :return:
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["top_rated"],
            params=params,
            key="results"
        )

    def upcoming(self, region=None, page=1):
        """
        Get a list of upcoming movies in theatres.
        :param region: str
        :param page: int
        :return:
        """
        params = "page=%s" % page
        if region:
            params += "&region=%s" % region
        return self._request_obj(
            self._urls["upcoming"],
            params=params,
            key="results"
        )
