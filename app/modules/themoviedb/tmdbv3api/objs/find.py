from ..tmdb import TMDb, TMDbJson


class Find(TMDb):
    """通过外部来源身份查询关联的 TMDb 条目。"""

    _urls = {
        "find": "/find/%s"
    }

    def find(self, external_id: int | str, external_source: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by an external id. For example, an IMDB ID.
        :param external_id: int | str
        :param external_source str
        :return:
        """
        if isinstance(external_id, int):
            external_id = str(external_id)
        return self._request_obj(
            self._urls["find"] % external_id.replace("/", "%2F"),
            params="external_source=" + external_source
        )

    def find_by_imdb_id(self, imdb_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by an IMDB ID.
        :param imdb_id: str
        :return:
        """
        return self.find(imdb_id, "imdb_id")

    def find_by_tvdb_id(self, tvdb_id: int | str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a TVDB ID.
        :param tvdb_id: int | str
        :return:
        """
        return self.find(tvdb_id, "tvdb_id")

    def find_by_freebase_mid(self, freebase_mid: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Freebase MID.
        :param freebase_mid: str
        :return:
        """
        return self.find(freebase_mid, "freebase_mid")

    def find_by_freebase_id(self, freebase_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Freebase ID.
        :param freebase_id: str
        :return:
        """
        return self.find(freebase_id, "freebase_id")

    def find_by_tvrage_id(self, tvrage_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a TVRage ID.
        :param tvrage_id: str
        :return:
        """
        return self.find(tvrage_id, "tvrage_id")

    def find_by_facebook_id(self, facebook_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Facebook ID.
        :param facebook_id: str
        :return:
        """
        return self.find(facebook_id, "facebook_id")

    def find_by_instagram_id(self, instagram_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Instagram ID.
        :param instagram_id: str
        :return:
        """
        return self.find(instagram_id, "instagram_id")

    def find_by_twitter_id(self, twitter_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Twitter ID.
        :param twitter_id: str
        :return:
        """
        return self.find(twitter_id, "twitter_id")

    # 异步版本方法
    async def async_find(self, external_id: int | str, external_source: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by an external id. For example, an IMDB ID.（异步版本）
        :param external_id: int | str
        :param external_source str
        :return:
        """
        if isinstance(external_id, int):
            external_id = str(external_id)
        return await self._async_request_obj(
            self._urls["find"] % external_id.replace("/", "%2F"),
            params="external_source=" + external_source
        )

    async def async_find_by_imdb_id(self, imdb_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by an IMDB ID.（异步版本）
        :param imdb_id: str
        :return:
        """
        return await self.async_find(imdb_id, "imdb_id")

    async def async_find_by_tvdb_id(self, tvdb_id: int | str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a TVDB ID.（异步版本）
        :param tvdb_id: int | str
        :return:
        """
        return await self.async_find(tvdb_id, "tvdb_id")

    async def async_find_by_freebase_mid(self, freebase_mid: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Freebase MID.（异步版本）
        :param freebase_mid: str
        :return:
        """
        return await self.async_find(freebase_mid, "freebase_mid")

    async def async_find_by_freebase_id(self, freebase_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Freebase ID.（异步版本）
        :param freebase_id: str
        :return:
        """
        return await self.async_find(freebase_id, "freebase_id")

    async def async_find_by_tvrage_id(self, tvrage_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a TVRage ID.（异步版本）
        :param tvrage_id: str
        :return:
        """
        return await self.async_find(tvrage_id, "tvrage_id")

    async def async_find_by_facebook_id(self, facebook_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Facebook ID.（异步版本）
        :param facebook_id: str
        :return:
        """
        return await self.async_find(facebook_id, "facebook_id")

    async def async_find_by_instagram_id(self, instagram_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Instagram ID.（异步版本）
        :param instagram_id: str
        :return:
        """
        return await self.async_find(instagram_id, "instagram_id")

    async def async_find_by_twitter_id(self, twitter_id: str) -> TMDbJson:
        """
        The find method makes it easy to search for objects in our database by a Twitter ID.（异步版本）
        :param twitter_id: str
        :return:
        """
        return await self.async_find(twitter_id, "twitter_id")
