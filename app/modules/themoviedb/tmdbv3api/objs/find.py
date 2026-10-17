"""
TMDB 外部标识接口的同步与异步调用封装。
"""

from ..tmdb import TMDb, TMDbJson


class Find(TMDb):
    """
    通过外部来源身份查询关联的 TMDb 条目。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "find": "/find/%s"
    }

    def find(self, external_id: int | str, external_source: str) -> TMDbJson:
        """
        按外部平台标识查找 TMDB 对应条目。

        :param external_id: 外部平台条目标识；数字先转为字符串，路径分隔符会编码
        :param external_source: 外部标识来源，如 imdb_id、tvdb_id
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        if isinstance(external_id, int):
            external_id = str(external_id)
        return self._request_obj(
            self._urls["find"] % external_id.replace("/", "%2F"),
            params="external_source=" + external_source
        )

    def find_by_imdb_id(self, imdb_id: str) -> TMDbJson:
        """
        按 IMDb 标识查找对应 TMDB 条目。

        :param imdb_id: IMDb 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(imdb_id, "imdb_id")

    def find_by_tvdb_id(self, tvdb_id: int | str) -> TMDbJson:
        """
        按 TVDB 标识查找对应 TMDB 条目。

        :param tvdb_id: TVDB 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(tvdb_id, "tvdb_id")

    def find_by_freebase_mid(self, freebase_mid: str) -> TMDbJson:
        """
        按 Freebase MID 标识查找对应 TMDB 条目。

        :param freebase_mid: Freebase MID 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(freebase_mid, "freebase_mid")

    def find_by_freebase_id(self, freebase_id: str) -> TMDbJson:
        """
        按 Freebase 标识查找对应 TMDB 条目。

        :param freebase_id: Freebase 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(freebase_id, "freebase_id")

    def find_by_tvrage_id(self, tvrage_id: str) -> TMDbJson:
        """
        按 TVRage 标识查找对应 TMDB 条目。

        :param tvrage_id: TVRage 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(tvrage_id, "tvrage_id")

    def find_by_facebook_id(self, facebook_id: str) -> TMDbJson:
        """
        按 Facebook 标识查找对应 TMDB 条目。

        :param facebook_id: Facebook 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(facebook_id, "facebook_id")

    def find_by_instagram_id(self, instagram_id: str) -> TMDbJson:
        """
        按 Instagram 标识查找对应 TMDB 条目。

        :param instagram_id: Instagram 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(instagram_id, "instagram_id")

    def find_by_twitter_id(self, twitter_id: str) -> TMDbJson:
        """
        按 Twitter 标识查找对应 TMDB 条目。

        :param twitter_id: Twitter 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self.find(twitter_id, "twitter_id")

    async def async_find(self, external_id: int | str, external_source: str) -> TMDbJson:
        """
        按外部平台标识查找 TMDB 对应条目。

        :param external_id: 外部平台条目标识；数字先转为字符串，路径分隔符会编码
        :param external_source: 外部标识来源，如 imdb_id、tvdb_id
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        if isinstance(external_id, int):
            external_id = str(external_id)
        return await self._async_request_obj(
            self._urls["find"] % external_id.replace("/", "%2F"),
            params="external_source=" + external_source
        )

    async def async_find_by_imdb_id(self, imdb_id: str) -> TMDbJson:
        """
        按 IMDb 标识查找对应 TMDB 条目。

        :param imdb_id: IMDb 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(imdb_id, "imdb_id")

    async def async_find_by_tvdb_id(self, tvdb_id: int | str) -> TMDbJson:
        """
        按 TVDB 标识查找对应 TMDB 条目。

        :param tvdb_id: TVDB 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(tvdb_id, "tvdb_id")

    async def async_find_by_freebase_mid(self, freebase_mid: str) -> TMDbJson:
        """
        按 Freebase MID 标识查找对应 TMDB 条目。

        :param freebase_mid: Freebase MID 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(freebase_mid, "freebase_mid")

    async def async_find_by_freebase_id(self, freebase_id: str) -> TMDbJson:
        """
        按 Freebase 标识查找对应 TMDB 条目。

        :param freebase_id: Freebase 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(freebase_id, "freebase_id")

    async def async_find_by_tvrage_id(self, tvrage_id: str) -> TMDbJson:
        """
        按 TVRage 标识查找对应 TMDB 条目。

        :param tvrage_id: TVRage 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(tvrage_id, "tvrage_id")

    async def async_find_by_facebook_id(self, facebook_id: str) -> TMDbJson:
        """
        按 Facebook 标识查找对应 TMDB 条目。

        :param facebook_id: Facebook 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(facebook_id, "facebook_id")

    async def async_find_by_instagram_id(self, instagram_id: str) -> TMDbJson:
        """
        按 Instagram 标识查找对应 TMDB 条目。

        :param instagram_id: Instagram 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(instagram_id, "instagram_id")

    async def async_find_by_twitter_id(self, twitter_id: str) -> TMDbJson:
        """
        按 Twitter 标识查找对应 TMDB 条目。

        :param twitter_id: Twitter 平台条目标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self.async_find(twitter_id, "twitter_id")
