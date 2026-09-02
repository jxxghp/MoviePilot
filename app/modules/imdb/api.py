"""IMDb 免 Key 数据接口客户端。"""

import asyncio
from typing import Any, Optional, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.runtime.cache import cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.tasks import get_task_registry

TModel = TypeVar("TModel", bound=BaseModel)


def _is_graphql_error(value: Optional[dict]) -> bool:
    """避免把 IMDb GraphQL 业务错误写入长期缓存。"""
    return bool(value and value.get("errors"))


class ImdbModel(BaseModel):
    """IMDb 响应模型基类，允许按字段名或外部别名构造。"""

    model_config = ConfigDict(populate_by_name=True)


class ImdbImage(ImdbModel):
    """IMDb 图片信息。"""

    url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    type: Optional[str] = None


class ImdbRating(ImdbModel):
    """IMDb 聚合评分信息。"""

    aggregate_rating: Optional[float] = Field(None, alias="aggregateRating")
    vote_count: Optional[int] = Field(None, alias="voteCount")


class ImdbDate(ImdbModel):
    """IMDb 可变精度日期。"""

    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None


class ImdbCountry(ImdbModel):
    """IMDb 制作国家信息。"""

    code: Optional[str] = None
    name: Optional[str] = None


class ImdbLanguage(ImdbModel):
    """IMDb 语言信息。"""

    code: Optional[str] = None
    name: Optional[str] = None


class ImdbPerson(ImdbModel):
    """IMDb 演职人员信息。"""

    id: Optional[str] = None
    display_name: Optional[str] = Field(None, alias="displayName")
    primary_image: Optional[ImdbImage] = Field(None, alias="primaryImage")


class ImdbTitle(ImdbModel):
    """IMDb 影视条目详情。"""

    id: str
    type: str
    is_adult: Optional[bool] = Field(None, alias="isAdult")
    primary_title: Optional[str] = Field(None, alias="primaryTitle")
    original_title: Optional[str] = Field(None, alias="originalTitle")
    primary_image: Optional[ImdbImage] = Field(None, alias="primaryImage")
    start_year: Optional[int] = Field(None, alias="startYear")
    end_year: Optional[int] = Field(None, alias="endYear")
    runtime_seconds: Optional[int] = Field(None, alias="runtimeSeconds")
    genres: list[str] = Field(default_factory=list)
    rating: Optional[ImdbRating] = None
    plot: Optional[str] = None
    directors: list[ImdbPerson] = Field(default_factory=list)
    writers: list[ImdbPerson] = Field(default_factory=list)
    stars: list[ImdbPerson] = Field(default_factory=list)
    origin_countries: list[ImdbCountry] = Field(
        default_factory=list, alias="originCountries"
    )
    spoken_languages: list[ImdbLanguage] = Field(
        default_factory=list, alias="spokenLanguages"
    )


class ImdbAka(ImdbModel):
    """IMDb 条目别名。"""

    text: Optional[str] = None


class ImdbEpisode(ImdbModel):
    """IMDb 单集信息。"""

    id: str
    title: Optional[str] = None
    primary_image: Optional[ImdbImage] = Field(None, alias="primaryImage")
    season: Optional[str] = None
    episode_number: Optional[int] = Field(None, alias="episodeNumber")
    runtime_seconds: Optional[int] = Field(None, alias="runtimeSeconds")
    plot: Optional[str] = None
    rating: Optional[ImdbRating] = None
    release_date: Optional[ImdbDate] = Field(None, alias="releaseDate")


class ImdbSeason(ImdbModel):
    """IMDb 季信息。"""

    season: Optional[str] = None
    episode_count: Optional[int] = Field(None, alias="episodeCount")


class ImdbCredit(ImdbModel):
    """IMDb 演职员表条目。"""

    name: Optional[ImdbPerson] = None
    category: Optional[str] = None
    characters: list[str] = Field(default_factory=list)


_TITLE_QUERY = """
query TitleDetails($titles: [ID!]!) {
  titles(ids: $titles) {
    id
    titleText { text }
    titleType { id }
    releaseYear { year }
    originalTitleText { text }
    primaryImage { url width height }
    ratingsSummary { aggregateRating voteCount }
    plot { plotText { plainText } }
    runtime { seconds }
    titleGenres { genres { genre { text } } }
    countriesOfOrigin { countries { id text } }
    spokenLanguages { spokenLanguages { id text } }
    isAdult
  }
}
"""

_AKAS_QUERY = """
query TitleAkas($titles: [ID!]!) {
  titles(ids: $titles) {
    akas(first: 50) { edges { node { text } } }
  }
}
"""

_CREDITS_QUERY = """
query TitleCredits($titles: [ID!]!) {
  titles(ids: $titles) {
    directors: credits(first: 5, filter: {categories: ["director"]}) {
      edges { node { name { id nameText { text } primaryImage { url } }
                     category { id } } }
    }
    cast: credits(first: 20, filter: {categories: ["actor", "actress"]}) {
      edges { node { name { id nameText { text } primaryImage { url } }
                     category { id } ... on Cast { characters { name } } } }
    }
  }
}
"""

_IMAGES_QUERY = """
query TitleImages($titles: [ID!]!) {
  titles(ids: $titles) {
    images(first: 50) { edges { node { url width height type } } }
  }
}
"""

_SEASONS_QUERY = """
query TitleSeasons($titles: [ID!]!) {
  titles(ids: $titles) { episodes { seasons { number } } }
}
"""

_EPISODES_QUERY = """
query TitleEpisodes($titles: [ID!]!, $first: Int!, $after: ID) {
  titles(ids: $titles) {
    episodes {
      episodes(first: $first, after: $after) {
        edges {
          node {
            id
            titleText { text }
            primaryImage { url width height }
            series { episodeNumber { episodeNumber seasonNumber } }
            runtime { seconds }
            plot { plotText { plainText } }
            ratingsSummary { aggregateRating voteCount }
            releaseDate { day month year }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class ImdbApi:
    """封装 IMDb 网页免 Key 接口的同步与异步只读访问。"""

    SEARCH_URL = "https://v2.sg.media-imdb.com/suggestion/x"
    GRAPHQL_URL = "https://caching.graphql.imdb.com/"

    def __init__(self, proxies: Optional[dict] = None) -> None:
        """按一次模块配置快照创建网络请求适配器。"""
        headers = {
            "User-Agent": get_runtime_setting('NORMAL_USER_AGENT'),
            "Accept": "application/graphql+json, application/json",
            "Content-Type": "application/json",
            "x-imdb-client-name": "imdb-web-next-localized",
        }
        self._request = RequestUtils(
            headers=headers,
            proxies=proxies,
            use_session=True,
            timeout=30,
        )
        self._async_request = AsyncRequestUtils(
            headers=headers,
            proxies=proxies,
            timeout=30,
        )

    @classmethod
    def _freeze_value(cls, value: Any) -> Any:
        """递归冻结请求参数，生成跨同步和异步实现一致的缓存键。"""
        if isinstance(value, dict):
            return tuple(
                sorted((key, cls._freeze_value(item)) for key, item in value.items())
            )
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze_value(item) for item in value)
        return value

    @classmethod
    def _freeze_params(cls, params: Optional[dict]) -> tuple[tuple[str, Any], ...]:
        """把请求参数冻结为可用于缓存键的有序元组。"""
        return cls._freeze_value(params or {})

    @cached(
        maxsize=get_runtime_setting('CONF').imdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="imdb_get",
    )
    def _cached_get_json(
        self, url: str, params_key: tuple[tuple[str, Any], ...]
    ) -> Optional[dict]:
        """同步读取并缓存一个 IMDb JSON GET 响应。"""
        return self._request.get_json(url, params=dict(params_key))

    @cached(
        maxsize=get_runtime_setting('CONF').imdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="imdb_get",
    )
    async def _async_cached_get_json(
        self, url: str, params_key: tuple[tuple[str, Any], ...]
    ) -> Optional[dict]:
        """异步读取并缓存一个 IMDb JSON GET 响应。"""
        return await self._async_request.get_json(url, params=dict(params_key))

    @cached(
        maxsize=get_runtime_setting('CONF').imdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="imdb_graphql",
        skip_if=_is_graphql_error,
    )
    def _cached_graphql(
        self, query: str, variables_key: tuple[tuple[str, Any], ...]
    ) -> Optional[dict]:
        """同步读取并缓存一个 IMDb GraphQL 响应。"""
        return self._request.post_json(
            self.GRAPHQL_URL,
            json={"query": query, "variables": dict(variables_key)},
        )

    @cached(
        maxsize=get_runtime_setting('CONF').imdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="imdb_graphql",
        skip_if=_is_graphql_error,
    )
    async def _async_cached_graphql(
        self, query: str, variables_key: tuple[tuple[str, Any], ...]
    ) -> Optional[dict]:
        """异步读取并缓存一个 IMDb GraphQL 响应。"""
        return await self._async_request.post_json(
            self.GRAPHQL_URL,
            json={"query": query, "variables": dict(variables_key)},
        )

    def _graphql(self, query: str, variables: dict) -> Optional[dict]:
        """同步执行 GraphQL 查询并提取 data 区域。"""
        response = self._cached_graphql(query, self._freeze_params(variables))
        if response and response.get("errors"):
            logger.debug("IMDb GraphQL 查询失败：%s", response["errors"])
            return None
        return response.get("data") if response else None

    async def _async_graphql(self, query: str, variables: dict) -> Optional[dict]:
        """异步执行 GraphQL 查询并提取 data 区域。"""
        response = await self._async_cached_graphql(
            query, self._freeze_params(variables)
        )
        if response and response.get("errors"):
            logger.debug("IMDb GraphQL 异步查询失败：%s", response["errors"])
            return None
        return response.get("data") if response else None

    def clear_cache(self) -> None:
        """清理同步缓存，并在当前或临时事件循环中清理异步缓存。"""
        self._cached_get_json.cache_clear()
        self._cached_graphql.cache_clear()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.async_clear_cache())
        else:
            # 同步 ABI 不能改成 async；运行中的循环交给宿主登记器收口，避免清理任务悬挂。
            try:
                get_task_registry().create(
                    self.async_clear_cache(),
                    owner="module.imdb.cache_clear",
                )
            except RuntimeError:
                # 兼容宿主已经进入关停阶段的同步调用：同步缓存已清理，异步缓存无需再启动新任务。
                return

    async def async_clear_cache(self) -> None:
        """清理 IMDb 异步 GET 与 GraphQL 请求缓存区。"""
        await self._async_cached_get_json.cache_clear()
        await self._async_cached_graphql.cache_clear()

    def close(self) -> None:
        """关闭同步 HTTP 会话，异步共享连接池由宿主统一关闭。"""
        self._request.close()

    @staticmethod
    def _parse(
        model: type[TModel], data: Optional[dict], operation: str
    ) -> Optional[TModel]:
        """把外部响应校验为内部模型，并隔离字段漂移错误。"""
        if not data:
            return None
        try:
            return model.model_validate(data)
        except (TypeError, ValueError, ValidationError) as err:
            logger.debug("解析 IMDb %s 响应失败：%s", operation, str(err))
            return None

    @staticmethod
    def _first_title(data: Optional[dict]) -> Optional[dict]:
        """从 IMDb GraphQL data 中提取第一个标题对象。"""
        titles = data.get("titles") if data else None
        return titles[0] if titles else None

    @classmethod
    def _parse_search_titles(cls, data: Optional[dict], limit: int) -> list[ImdbTitle]:
        """把 IMDb 搜索建议响应转换为稳定标题模型。"""
        titles: list[ImdbTitle] = []
        for item in (data or {}).get("d", []):
            imdb_id = item.get("id")
            title_type = item.get("qid")
            if not imdb_id or not imdb_id.startswith("tt") or not title_type:
                continue
            image = item.get("i") or {}
            title = cls._parse(
                ImdbTitle,
                {
                    "id": imdb_id,
                    "type": title_type,
                    "primaryTitle": item.get("l"),
                    "primaryImage": {
                        "url": image.get("imageUrl"),
                        "width": image.get("width"),
                        "height": image.get("height"),
                    },
                    "startYear": item.get("y"),
                },
                "标题搜索",
            )
            if title:
                titles.append(title)
            if len(titles) >= limit:
                break
        return titles

    @classmethod
    def _parse_title(cls, data: Optional[dict]) -> Optional[ImdbTitle]:
        """把 IMDb GraphQL 标题结构展平为内部详情模型。"""
        item = cls._first_title(data)
        if not item:
            return None
        genres = (item.get("titleGenres") or {}).get("genres") or []
        countries = (item.get("countriesOfOrigin") or {}).get("countries") or []
        languages = (item.get("spokenLanguages") or {}).get("spokenLanguages") or []
        return cls._parse(
            ImdbTitle,
            {
                "id": item.get("id"),
                "type": (item.get("titleType") or {}).get("id"),
                "isAdult": item.get("isAdult"),
                "primaryTitle": (item.get("titleText") or {}).get("text"),
                "originalTitle": (item.get("originalTitleText") or {}).get(
                    "text"
                ),
                "primaryImage": item.get("primaryImage"),
                "startYear": (item.get("releaseYear") or {}).get("year"),
                "runtimeSeconds": (item.get("runtime") or {}).get("seconds"),
                "genres": [
                    (entry.get("genre") or {}).get("text")
                    for entry in genres
                    if (entry.get("genre") or {}).get("text")
                ],
                "rating": item.get("ratingsSummary"),
                "plot": ((item.get("plot") or {}).get("plotText") or {}).get(
                    "plainText"
                ),
                "originCountries": [
                    {"code": entry.get("id"), "name": entry.get("text")}
                    for entry in countries
                ],
                "spokenLanguages": [
                    {"code": entry.get("id"), "name": entry.get("text")}
                    for entry in languages
                ],
            },
            "标题详情",
        )

    @classmethod
    def _parse_akas(cls, data: Optional[dict]) -> list[ImdbAka]:
        """解析 GraphQL 标题别名边。"""
        title = cls._first_title(data) or {}
        edges = title.get("akas", {}).get("edges") or []
        return [
            aka
            for edge in edges
            if (aka := cls._parse(ImdbAka, edge.get("node"), "标题别名"))
        ]

    @classmethod
    def _parse_credits(cls, data: Optional[dict]) -> list[ImdbCredit]:
        """解析 GraphQL 导演和主演边。"""
        title = cls._first_title(data) or {}
        credits: list[ImdbCredit] = []
        for field_name in ("directors", "cast"):
            for edge in title.get(field_name, {}).get("edges") or []:
                node = edge.get("node") or {}
                name = node.get("name") or {}
                credit = cls._parse(
                    ImdbCredit,
                    {
                        "name": {
                            "id": name.get("id"),
                            "displayName": name.get("nameText", {}).get("text"),
                            "primaryImage": name.get("primaryImage"),
                        },
                        "category": node.get("category", {}).get("id"),
                        "characters": [
                            item.get("name")
                            for item in node.get("characters") or []
                            if item.get("name")
                        ],
                    },
                    "演职员",
                )
                if credit:
                    credits.append(credit)
        return credits

    @classmethod
    def _parse_images(cls, data: Optional[dict]) -> list[ImdbImage]:
        """解析 GraphQL 标题图片边。"""
        title = cls._first_title(data) or {}
        edges = title.get("images", {}).get("edges") or []
        return [
            image
            for edge in edges
            if (image := cls._parse(ImdbImage, edge.get("node"), "标题图片"))
        ]

    @classmethod
    def _parse_seasons(cls, data: Optional[dict]) -> list[ImdbSeason]:
        """解析 GraphQL 季编号列表。"""
        title = cls._first_title(data) or {}
        values = title.get("episodes", {}).get("seasons") or []
        return [ImdbSeason(season=str(item["number"])) for item in values]

    @classmethod
    def _parse_episode_page(
        cls, data: Optional[dict]
    ) -> tuple[list[ImdbEpisode], Optional[str]]:
        """解析一页 GraphQL 单集边及下一页游标。"""
        title = cls._first_title(data) or {}
        connection = title.get("episodes", {}).get("episodes") or {}
        episodes: list[ImdbEpisode] = []
        for edge in connection.get("edges") or []:
            node = edge.get("node") or {}
            episode_number = node.get("series", {}).get("episodeNumber") or {}
            episode = cls._parse(
                ImdbEpisode,
                {
                    "id": node.get("id"),
                    "title": node.get("titleText", {}).get("text"),
                    "primaryImage": node.get("primaryImage"),
                    "season": str(episode_number.get("seasonNumber"))
                    if episode_number.get("seasonNumber") is not None
                    else None,
                    "episodeNumber": episode_number.get("episodeNumber"),
                    "runtimeSeconds": node.get("runtime", {}).get("seconds"),
                    "plot": node.get("plot", {}).get("plotText", {}).get("plainText"),
                    "rating": node.get("ratingsSummary"),
                    "releaseDate": node.get("releaseDate"),
                },
                "单集详情",
            )
            if episode:
                episodes.append(episode)
        page_info = connection.get("pageInfo") or {}
        next_cursor = (
            page_info.get("endCursor") if page_info.get("hasNextPage") else None
        )
        return episodes, next_cursor

    def search_titles(self, query: str, limit: int = 50) -> list[ImdbTitle]:
        """按标题关键字搜索 IMDb 影视条目。"""
        url = f"{self.SEARCH_URL}/{quote(query.strip(), safe='')}.json"
        data = self._cached_get_json(url, self._freeze_params(None))
        return self._parse_search_titles(data, limit)

    async def async_search_titles(
        self, query: str, limit: int = 50
    ) -> list[ImdbTitle]:
        """异步按标题关键字搜索 IMDb 影视条目。"""
        url = f"{self.SEARCH_URL}/{quote(query.strip(), safe='')}.json"
        data = await self._async_cached_get_json(url, self._freeze_params(None))
        return self._parse_search_titles(data, limit)

    def get_title(self, imdb_id: str) -> Optional[ImdbTitle]:
        """按 IMDb ID 获取影视条目详情。"""
        return self._parse_title(self._graphql(_TITLE_QUERY, {"titles": [imdb_id]}))

    async def async_get_title(self, imdb_id: str) -> Optional[ImdbTitle]:
        """异步按 IMDb ID 获取影视条目详情。"""
        return self._parse_title(
            await self._async_graphql(_TITLE_QUERY, {"titles": [imdb_id]})
        )

    def list_akas(self, imdb_id: str) -> list[ImdbAka]:
        """获取影视条目的可用别名。"""
        return self._parse_akas(self._graphql(_AKAS_QUERY, {"titles": [imdb_id]}))

    async def async_list_akas(self, imdb_id: str) -> list[ImdbAka]:
        """异步获取影视条目的可用别名。"""
        return self._parse_akas(
            await self._async_graphql(_AKAS_QUERY, {"titles": [imdb_id]})
        )

    def list_episodes(self, imdb_id: str) -> list[ImdbEpisode]:
        """使用游标分页获取电视剧条目的全部单集。"""
        episodes: list[ImdbEpisode] = []
        cursor: Optional[str] = None
        while True:
            data = self._graphql(
                _EPISODES_QUERY,
                {"titles": [imdb_id], "first": 100, "after": cursor},
            )
            page, cursor = self._parse_episode_page(data)
            episodes.extend(page)
            if not cursor:
                return episodes

    async def async_list_episodes(self, imdb_id: str) -> list[ImdbEpisode]:
        """异步使用游标分页获取电视剧条目的全部单集。"""
        episodes: list[ImdbEpisode] = []
        cursor: Optional[str] = None
        while True:
            data = await self._async_graphql(
                _EPISODES_QUERY,
                {"titles": [imdb_id], "first": 100, "after": cursor},
            )
            page, cursor = self._parse_episode_page(data)
            episodes.extend(page)
            if not cursor:
                return episodes

    def list_seasons(self, imdb_id: str) -> list[ImdbSeason]:
        """获取电视剧条目的季列表。"""
        return self._parse_seasons(
            self._graphql(_SEASONS_QUERY, {"titles": [imdb_id]})
        )

    async def async_list_seasons(self, imdb_id: str) -> list[ImdbSeason]:
        """异步获取电视剧条目的季列表。"""
        return self._parse_seasons(
            await self._async_graphql(_SEASONS_QUERY, {"titles": [imdb_id]})
        )

    def list_credits(self, imdb_id: str) -> list[ImdbCredit]:
        """获取影视条目的导演与主要演员。"""
        return self._parse_credits(
            self._graphql(_CREDITS_QUERY, {"titles": [imdb_id]})
        )

    async def async_list_credits(self, imdb_id: str) -> list[ImdbCredit]:
        """异步获取影视条目的导演与主要演员。"""
        return self._parse_credits(
            await self._async_graphql(_CREDITS_QUERY, {"titles": [imdb_id]})
        )

    def list_images(self, imdb_id: str) -> list[ImdbImage]:
        """获取影视条目的主要图片。"""
        return self._parse_images(
            self._graphql(_IMAGES_QUERY, {"titles": [imdb_id]})
        )

    async def async_list_images(self, imdb_id: str) -> list[ImdbImage]:
        """异步获取影视条目的主要图片。"""
        return self._parse_images(
            await self._async_graphql(_IMAGES_QUERY, {"titles": [imdb_id]})
        )
