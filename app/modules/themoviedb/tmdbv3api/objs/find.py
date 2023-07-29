from ..tmdb import TMDb


class Find(TMDb):
    _urls = {
        "find": "/find/%s"
    }

    def find(self, external_id, external_source):
        """
        The find method makes it easy to search for objects in our database by an external id. For example, an IMDB ID.
        :param external_id: str
        :param external_source str
        :return:
        """
        return self._request_obj(
            self._urls["find"] % external_id.replace("/", "%2F"),
            params="external_source=" + external_source
        )

    def find_by_imdb_id(self, imdb_id):
        """
        The find method makes it easy to search for objects in our database by an IMDB ID.
        :param imdb_id: str
        :return:
        """
        return self.find(imdb_id, "imdb_id")

    def find_by_tvdb_id(self, tvdb_id):
        """
        The find method makes it easy to search for objects in our database by a TVDB ID.
        :param tvdb_id: int
        :return:
        """
        return self.find(tvdb_id, "tvdb_id")

    def find_by_freebase_mid(self, freebase_mid):
        """
        The find method makes it easy to search for objects in our database by a Freebase MID.
        :param freebase_mid: str
        :return:
        """
        return self.find(freebase_mid, "freebase_mid")

    def find_by_freebase_id(self, freebase_id):
        """
        The find method makes it easy to search for objects in our database by a Freebase ID.
        :param freebase_id: str
        :return:
        """
        return self.find(freebase_id, "freebase_id")

    def find_by_tvrage_id(self, tvrage_id):
        """
        The find method makes it easy to search for objects in our database by a TVRage ID.
        :param tvrage_id: str
        :return:
        """
        return self.find(tvrage_id, "tvrage_id")

    def find_by_facebook_id(self, facebook_id):
        """
        The find method makes it easy to search for objects in our database by a Facebook ID.
        :param facebook_id: str
        :return:
        """
        return self.find(facebook_id, "facebook_id")

    def find_by_instagram_id(self, instagram_id):
        """
        The find method makes it easy to search for objects in our database by a Instagram ID.
        :param instagram_id: str
        :return:
        """
        return self.find(instagram_id, "instagram_id")

    def find_by_twitter_id(self, twitter_id):
        """
        The find method makes it easy to search for objects in our database by a Twitter ID.
        :param twitter_id: str
        :return:
        """
        return self.find(twitter_id, "twitter_id")
