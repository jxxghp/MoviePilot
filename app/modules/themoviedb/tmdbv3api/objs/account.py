"""
TMDB 账号接口的同步与异步调用封装。
"""

import os

from ..exceptions import TMDbException
from ..tmdb import TMDb, TMDbJson


class Account(TMDb):
    """
    提供 TMDb 账号详情、收藏、评分和待看列表接口。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """

    _urls = {
        "details": "/account",
        "created_lists": "/account/%s/lists",
        "favorite_movies": "/account/%s/favorite/movies",
        "favorite_tv": "/account/%s/favorite/tv",
        "favorite": "/account/%s/favorite",
        "rated_movies": "/account/%s/rated/movies",
        "rated_tv": "/account/%s/rated/tv",
        "rated_episodes": "/account/%s/rated/tv/episodes",
        "movie_watchlist": "/account/%s/watchlist/movies",
        "tv_watchlist": "/account/%s/watchlist/tv",
        "watchlist": "/account/%s/watchlist",
    }

    @property
    def account_id(self) -> str | None:
        """
        读取缓存的账号 ID；未配置时沿用账号详情查询及环境变量缓存。
        """
        if not os.environ.get("TMDB_ACCOUNT_ID"):
            details = self.details()
            if not isinstance(details, dict):
                raise TypeError("TMDb account details must be a JSON object")
            os.environ["TMDB_ACCOUNT_ID"] = str(details["id"])
        return os.environ.get("TMDB_ACCOUNT_ID")

    def details(self) -> TMDbJson:
        """
        查询账号详情。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(
            self._urls["details"],
            params="session_id=%s" % self.session_id
        )

    async def async_details(self) -> TMDbJson:
        """
        查询账号详情。

        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(
            self._urls["details"],
            params="session_id=%s" % self.session_id
        )

    def created_lists(self, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号创建的片单，包含本人可见的私有片单。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(
            self._urls["created_lists"] % self.account_id,
            params="session_id=%s&page=%s" % (self.session_id, page),
            key="results"
        )

    async def async_created_lists(self, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号创建的片单，包含本人可见的私有片单。

        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(
            self._urls["created_lists"] % self.account_id,
            params="session_id=%s&page=%s" % (self.session_id, page),
            key="results"
        )

    def _get_list(self, url: str, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        按账号、分页和排序条件查询指定列表。

        :param url: 账号列表在 _urls 中的键名
        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "session_id=%s&page=%s" % (self.session_id, page)
        if asc_sort is False:
            params += "&sort_by=created_at.desc"
        return self._request_obj(
            self._urls[url] % self.account_id,
            params=params,
            key="results"
        )

    async def _async_get_list(self, url: str, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        按账号、分页和排序条件查询指定列表。

        :param url: 账号列表在 _urls 中的键名
        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        params = "session_id=%s&page=%s" % (self.session_id, page)
        if asc_sort is False:
            params += "&sort_by=created_at.desc"
        return await self._async_request_obj(
            self._urls[url] % self.account_id,
            params=params,
            key="results"
        )

    def favorite_movies(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号收藏的电影。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("favorite_movies", asc_sort=asc_sort, page=page)

    async def async_favorite_movies(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号收藏的电影。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("favorite_movies", asc_sort=asc_sort, page=page)

    def favorite_tv_shows(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号收藏的电视剧。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("favorite_tv", asc_sort=asc_sort, page=page)

    async def async_favorite_tv_shows(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号收藏的电视剧。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("favorite_tv", asc_sort=asc_sort, page=page)

    def mark_as_favorite(self, media_id: int | str, media_type: str, favorite: bool = True) -> None:
        """
        设置当前账号的媒体收藏状态。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :param favorite: True 加入收藏，False 取消收藏
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        self._request_obj(
            self._urls["favorite"] % self.account_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "favorite": favorite,
            }
        )

    async def async_mark_as_favorite(self, media_id: int | str, media_type: str, favorite: bool = True) -> None:
        """
        设置当前账号的媒体收藏状态。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :param favorite: True 加入收藏，False 取消收藏
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        await self._async_request_obj(
            self._urls["favorite"] % self.account_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "favorite": favorite,
            }
        )

    def unmark_as_favorite(self, media_id: int | str, media_type: str) -> None:
        """
        取消当前账号的媒体收藏。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        self.mark_as_favorite(media_id, media_type, favorite=False)

    async def async_unmark_as_favorite(self, media_id: int | str, media_type: str) -> None:
        """
        取消当前账号的媒体收藏。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        await self.async_mark_as_favorite(media_id, media_type, favorite=False)

    def rated_movies(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的电影。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("rated_movies", asc_sort=asc_sort, page=page)

    async def async_rated_movies(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的电影。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("rated_movies", asc_sort=asc_sort, page=page)

    def rated_tv_shows(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的电视剧。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("rated_tv", asc_sort=asc_sort, page=page)

    async def async_rated_tv_shows(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的电视剧。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("rated_tv", asc_sort=asc_sort, page=page)

    def rated_episodes(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的单集。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("rated_episodes", asc_sort=asc_sort, page=page)

    async def async_rated_episodes(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号已评分的单集。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("rated_episodes", asc_sort=asc_sort, page=page)

    def movie_watchlist(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号的电影待看列表。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("movie_watchlist", asc_sort=asc_sort, page=page)

    async def async_movie_watchlist(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号的电影待看列表。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("movie_watchlist", asc_sort=asc_sort, page=page)

    def tv_show_watchlist(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号的电视剧待看列表。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._get_list("tv_watchlist", asc_sort=asc_sort, page=page)

    async def async_tv_show_watchlist(self, asc_sort: bool = True, page: int | None = 1) -> TMDbJson:
        """
        查询当前账号的电视剧待看列表。

        :param asc_sort: True 保留接口默认顺序，False 按创建时间降序
        :param page: 请求页码，从 1 开始；仅取该页，不自动遍历后续页
        :return: 响应的 results 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_get_list("tv_watchlist", asc_sort=asc_sort, page=page)

    def add_to_watchlist(self, media_id: int | str, media_type: str, watchlist: bool = True) -> None:
        """
        设置媒体是否加入当前账号的待看列表。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :param watchlist: True 加入待看列表，False 移除
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        self._request_obj(
            self._urls["watchlist"] % self.account_id,
            "session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "watchlist": watchlist,
            }
        )

    async def async_add_to_watchlist(self, media_id: int | str, media_type: str, watchlist: bool = True) -> None:
        """
        设置媒体是否加入当前账号的待看列表。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :param watchlist: True 加入待看列表，False 移除
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        if media_type not in ["tv", "movie"]:
            raise TMDbException("Media Type should be tv or movie.")
        await self._async_request_obj(
            self._urls["watchlist"] % self.account_id,
            "session_id=%s" % self.session_id,
            method="POST",
            json={
                "media_type": media_type,
                "media_id": media_id,
                "watchlist": watchlist,
            }
        )

    def remove_from_watchlist(self, media_id: int | str, media_type: str) -> None:
        """
        从当前账号的待看列表移除媒体。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        self.add_to_watchlist(media_id, media_type, watchlist=False)

    async def async_remove_from_watchlist(self, media_id: int | str, media_type: str) -> None:
        """
        从当前账号的待看列表移除媒体。

        :param media_id: 目标媒体的 TMDB 标识
        :param media_type: 媒体类型，movie 表示电影，tv 表示电视剧
        :raises TMDbException: 媒体类型不是 movie 或 tv，或服务端拒绝操作
        """
        await self.async_add_to_watchlist(media_id, media_type, watchlist=False)
