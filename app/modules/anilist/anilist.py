from typing import Optional

from app.core.cache import cached
from app.core.config import settings
from app.log import logger
from app.utils.http import AsyncRequestUtils, RequestUtils


class AniListApi:
    """
    AniList GraphQL API 客户端
    """

    _base_url = "https://graphql.anilist.co"
    _media_fields = """
        id
        idMal
        title { romaji english native }
        format
        status
        description(asHtml: false)
        startDate { year month day }
        endDate { year month day }
        seasonYear
        episodes
        duration
        countryOfOrigin
        coverImage { extraLarge large }
        bannerImage
        genres
        synonyms
        averageScore
        popularity
        isAdult
        siteUrl
        studios(isMain: true) { nodes { name } }
        staff(perPage: 25, sort: [RELEVANCE]) {
          edges { role node { name { full } image { large } siteUrl } }
        }
        characters(perPage: 25, sort: [ROLE]) {
          edges {
            role
            node { name { full native } image { large } siteUrl }
            voiceActors(language: JAPANESE, sort: [RELEVANCE]) {
              name { full }
              image { large }
              siteUrl
            }
          }
        }
        externalLinks { site url type }
    """

    def __init__(self) -> None:
        """初始化同步与异步请求客户端"""
        headers = {
            "User-Agent": settings.NORMAL_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._request = RequestUtils(
            proxies=settings.PROXY,
            headers=headers,
        )
        self._async_request = AsyncRequestUtils(
            proxies=settings.PROXY,
            headers=headers,
        )

    @staticmethod
    def _extract_response(response) -> Optional[dict]:
        """
        提取 GraphQL 响应数据并统一处理上游错误。

        :param response: HTTP 响应对象
        :return: GraphQL data 字段
        """
        if response is None or response.status_code != 200:
            return None
        try:
            result = response.json()
        except Exception as err:
            logger.error(f"解析 AniList 响应失败：{str(err)}")
            return None
        if result.get("errors"):
            logger.warning(f"AniList 接口返回错误：{result.get('errors')}")
            return None
        return result.get("data")

    def _invoke(self, query: str, variables: dict) -> Optional[dict]:
        """
        执行同步 GraphQL 请求。

        :param query: GraphQL 查询
        :param variables: 查询变量
        :return: GraphQL data 字段
        """
        response = self._request.post_res(
            self._base_url,
            json={"query": query, "variables": variables},
        )
        return self._extract_response(response)

    async def _async_invoke(self, query: str, variables: dict) -> Optional[dict]:
        """
        执行异步 GraphQL 请求。

        :param query: GraphQL 查询
        :param variables: 查询变量
        :return: GraphQL data 字段
        """
        response = await self._async_request.post_res(
            self._base_url,
            json={"query": query, "variables": variables},
        )
        return self._extract_response(response)

    @cached(maxsize=settings.CONF.anilist, ttl=settings.CONF.meta, shared_key="get")
    def detail(self, anilist_id: int) -> Optional[dict]:
        """
        根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = self._invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(maxsize=settings.CONF.anilist, ttl=settings.CONF.meta, shared_key="get")
    async def async_detail(self, anilist_id: int) -> Optional[dict]:
        """
        异步根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = await self._async_invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(maxsize=settings.CONF.anilist, ttl=settings.CONF.meta, shared_key="get")
    def search(self, name: str, count: int = 20) -> list[dict]:
        """
        按标题搜索 AniList 动画。

        :param name: 动画标题
        :param count: 返回条数
        :return: AniList 媒体列表
        """
        query = f"""
            query ($search: String!, $count: Int!) {{
              Page(page: 1, perPage: $count) {{
                media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{ {self._media_fields} }}
              }}
            }}
        """
        result = self._invoke(query, {"search": name, "count": count})
        return result.get("Page", {}).get("media") or [] if result else []

    @cached(maxsize=settings.CONF.anilist, ttl=settings.CONF.meta, shared_key="get")
    async def async_search(self, name: str, count: int = 20) -> list[dict]:
        """
        异步按标题搜索 AniList 动画。

        :param name: 动画标题
        :param count: 返回条数
        :return: AniList 媒体列表
        """
        query = f"""
            query ($search: String!, $count: Int!) {{
              Page(page: 1, perPage: $count) {{
                media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{ {self._media_fields} }}
              }}
            }}
        """
        result = await self._async_invoke(query, {"search": name, "count": count})
        return result.get("Page", {}).get("media") or [] if result else []

    def clear_cache(self) -> None:
        """清理 AniList 详情与搜索缓存"""
        self.detail.cache_clear()
        self.async_detail.cache_clear()
        self.search.cache_clear()
        self.async_search.cache_clear()
