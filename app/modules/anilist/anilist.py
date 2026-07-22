from datetime import date
from typing import Optional

from app.core.cache import cached
from app.core.config import settings
from app.log import logger
from app.utils.http import AsyncRequestUtils, RequestUtils


class AniListApi:
    """
    AniList 中文 GraphQL API 客户端
    """

    _base_url = "https://trace.moe/anilist/"
    _official_url = "https://graphql.anilist.co"
    _translations_url = (
        "https://raw.githubusercontent.com/soruly/anilist-chinese/"
        "master/anilist-chinese.json"
    )
    _media_summary_fields = """
        id
        idMal
        title { romaji english native }
        format
        status
        description(asHtml: false)
        startDate { year month day }
        endDate { year month day }
        season
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
    """
    _media_fields = f"""
        {_media_summary_fields}
        staff(perPage: 25, sort: [RELEVANCE]) {{
          edges {{ role node {{ id name {{ full native }} image {{ large }} siteUrl }} }}
        }}
        characters(perPage: 25, sort: [ROLE, RELEVANCE]) {{
          edges {{
            role
            node {{ id name {{ full native }} image {{ large }} siteUrl }}
            voiceActors(language: JAPANESE, sort: [RELEVANCE]) {{
              id
              name {{ full native alternative }}
              image {{ large medium }}
              siteUrl
            }}
          }}
        }}
        externalLinks {{ site url type }}
    """
    _page_query = f"""
        query (
          $page: Int!,
          $count: Int!,
          $search: String,
          $genre: String,
          $format: MediaFormat,
          $season: MediaSeason,
          $seasonYear: Int,
          $status: MediaStatus,
          $country: CountryCode,
          $sort: [MediaSort]
        ) {{
          Page(page: $page, perPage: $count) {{
            media(
              search: $search,
              type: ANIME,
              genre: $genre,
              format: $format,
              season: $season,
              seasonYear: $seasonYear,
              status: $status,
              countryOfOrigin: $country,
              isAdult: false,
              sort: $sort
            ) {{ {_media_summary_fields} }}
          }}
        }}
    """
    _media_by_ids_query = f"""
        query ($ids: [Int!]!, $count: Int!) {{
          Page(page: 1, perPage: $count) {{
            media(id_in: $ids, type: ANIME) {{ {_media_summary_fields} }}
          }}
        }}
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
        self._proxy_available = True
        self._translations: Optional[dict[int, dict]] = None

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
        payload = {"query": query, "variables": variables}
        if self._proxy_available:
            response = self._request.post_res(self._base_url, json=payload)
            result = self._extract_response(response)
            if result is not None:
                return self._inject_chinese(result, self._translation_map())
            self._disable_proxy(response)
        response = self._request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        return self._inject_chinese(result, self._translation_map()) if result else result

    async def _async_invoke(self, query: str, variables: dict) -> Optional[dict]:
        """
        执行异步 GraphQL 请求。

        :param query: GraphQL 查询
        :param variables: 查询变量
        :return: GraphQL data 字段
        """
        payload = {"query": query, "variables": variables}
        if self._proxy_available:
            response = await self._async_request.post_res(self._base_url, json=payload)
            result = self._extract_response(response)
            if result is not None:
                translations = await self._async_translation_map()
                return self._inject_chinese(result, translations)
            self._disable_proxy(response)
        response = await self._async_request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        if not result:
            return result
        translations = await self._async_translation_map()
        return self._inject_chinese(result, translations)

    def _disable_proxy(self, response) -> None:
        """
        标记中文代理不可用，避免当前进程持续请求已失效的上游。

        :param response: 中文代理响应对象
        """
        self._proxy_available = False
        status_code = getattr(response, "status_code", None)
        logger.warning(
            f"anilist-chinese 代理不可用（HTTP {status_code}），"
            "改用 AniList 官方接口并合并中文数据集"
        )

    @staticmethod
    def _build_translation_map(items) -> dict[int, dict]:
        """
        将 anilist-chinese 数据集转换为按 AniList ID 索引的字典。

        :param items: anilist-chinese JSON 数据
        :return: 中文标题数据索引
        """
        if not isinstance(items, list):
            return {}
        return {
            item.get("id"): item
            for item in items
            if isinstance(item, dict) and item.get("id")
        }

    def _translation_map(self) -> dict[int, dict]:
        """
        同步加载并复用 anilist-chinese 中文标题数据。

        :return: 中文标题数据索引
        """
        if self._translations is None:
            items = self._request.get_json(self._translations_url)
            self._translations = self._build_translation_map(items)
            if not self._translations:
                logger.warning("加载 anilist-chinese 中文数据集失败")
        return self._translations

    async def _async_translation_map(self) -> dict[int, dict]:
        """
        异步加载并复用 anilist-chinese 中文标题数据。

        :return: 中文标题数据索引
        """
        if self._translations is None:
            items = await self._async_request.get_json(self._translations_url)
            self._translations = self._build_translation_map(items)
            if not self._translations:
                logger.warning("加载 anilist-chinese 中文数据集失败")
        return self._translations

    @classmethod
    def _inject_chinese(cls, value, translations: dict[int, dict]):
        """
        递归合并 anilist-chinese 标题，覆盖代理不会处理的嵌套媒体。

        :param value: AniList GraphQL data 字段或其子节点
        :param translations: 中文标题数据索引
        :return: 合并中文标题后的原数据结构
        """
        if isinstance(value, list):
            for item in value:
                cls._inject_chinese(item, translations)
            return value
        if not isinstance(value, dict):
            return value

        translation = translations.get(value.get("id"))
        title = value.get("title")
        if translation and isinstance(title, dict):
            title["chinese"] = translation.get("title")
        synonyms = value.get("synonyms")
        if translation and isinstance(synonyms, list):
            value["synonyms"] = list(
                dict.fromkeys([*synonyms, *(translation.get("synonyms") or [])])
            )
        for child in value.values():
            cls._inject_chinese(child, translations)
        return value

    @staticmethod
    def _page_variables(
        page: int,
        count: int,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> dict:
        """
        构造 AniList 分页媒体查询变量。

        :return: 去除空值后的 GraphQL 变量
        """
        variables = {
            "page": page,
            "count": count,
            "search": search,
            "genre": genre,
            "format": media_format,
            "season": season,
            "seasonYear": season_year,
            "status": status,
            "country": country,
            "sort": [sort] if sort else ["POPULARITY_DESC"],
        }
        return {key: value for key, value in variables.items() if value is not None}

    @staticmethod
    def _page_medias(result: Optional[dict]) -> list[dict]:
        """
        从分页响应中提取媒体列表。

        :param result: GraphQL data 字段
        :return: AniList 媒体列表
        """
        return result.get("Page", {}).get("media") or [] if result else []

    @staticmethod
    def _ordered_medias(media_ids: list[int], medias: list[dict]) -> list[dict]:
        """
        按上游关系顺序重排批量查询返回的媒体。

        :param media_ids: 关系查询返回的 AniList 媒体 ID
        :param medias: Page.media 批量查询结果
        :return: 保持原关系顺序的媒体列表
        """
        media_map = {media.get("id"): media for media in medias if media.get("id")}
        return [media_map[media_id] for media_id in media_ids if media_id in media_map]

    def _medias_by_ids(self, media_ids: list[int]) -> list[dict]:
        """
        通过根级 Page.media 批量查询媒体，使中文代理能够注入标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        if not unique_ids:
            return []
        result = self._invoke(
            self._media_by_ids_query,
            {"ids": unique_ids, "count": len(unique_ids)},
        )
        return self._ordered_medias(media_ids, self._page_medias(result))

    async def _async_medias_by_ids(self, media_ids: list[int]) -> list[dict]:
        """
        异步通过根级 Page.media 批量查询媒体，使中文代理能够注入标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        if not unique_ids:
            return []
        result = await self._async_invoke(
            self._media_by_ids_query,
            {"ids": unique_ids, "count": len(unique_ids)},
        )
        return self._ordered_medias(media_ids, self._page_medias(result))

    @staticmethod
    def _current_season(today: Optional[date] = None) -> tuple[str, int]:
        """
        根据当前日期计算 AniList 季度与年份。

        :param today: 用于测试或指定季度的日期
        :return: AniList 季度枚举和年份
        """
        current = today or date.today()
        seasons = ("WINTER", "SPRING", "SUMMER", "FALL")
        return seasons[(current.month - 1) // 3], current.year

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="detail",
    )
    def detail(self, anilist_id: int) -> Optional[dict]:
        """
        根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = self._invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="detail",
    )
    async def async_detail(self, anilist_id: int) -> Optional[dict]:
        """
        异步根据 AniList ID 获取动画详情。

        :param anilist_id: AniList 媒体 ID
        :return: AniList 媒体详情
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = await self._async_invoke(query, {"id": anilist_id})
        return result.get("Media") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="search",
    )
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
        return self._page_medias(result)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="search",
    )
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
        return self._page_medias(result)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="discover",
    )
    def discover(
        self,
        page: int = 1,
        count: int = 20,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict]:
        """
        按组合条件探索 AniList 动画。

        :return: AniList 媒体列表
        """
        variables = self._page_variables(
            page=page,
            count=count,
            search=search,
            genre=genre,
            media_format=media_format,
            season=season,
            season_year=season_year,
            status=status,
            country=country,
            sort=sort,
        )
        return self._page_medias(self._invoke(self._page_query, variables))

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="discover",
    )
    async def async_discover(
        self,
        page: int = 1,
        count: int = 20,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        media_format: Optional[str] = None,
        season: Optional[str] = None,
        season_year: Optional[int] = None,
        status: Optional[str] = None,
        country: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict]:
        """
        异步按组合条件探索 AniList 动画。

        :return: AniList 媒体列表
        """
        variables = self._page_variables(
            page=page,
            count=count,
            search=search,
            genre=genre,
            media_format=media_format,
            season=season,
            season_year=season_year,
            status=status,
            country=country,
            sort=sort,
        )
        result = await self._async_invoke(self._page_query, variables)
        return self._page_medias(result)

    def trending(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 当前趋势榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        return self.discover(page=page, count=count, sort="TRENDING_DESC")

    async def async_trending(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 当前趋势榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        return await self.async_discover(page=page, count=count, sort="TRENDING_DESC")

    def popular_this_season(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 本季热门榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        season, season_year = self._current_season()
        return self.discover(
            page=page,
            count=count,
            season=season,
            season_year=season_year,
            sort="POPULARITY_DESC",
        )

    async def async_popular_this_season(self, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 本季热门榜。

        :param page: 页码
        :param count: 每页条数
        :return: AniList 媒体列表
        """
        season, season_year = self._current_season()
        return await self.async_discover(
            page=page,
            count=count,
            season=season,
            season_year=season_year,
            sort="POPULARITY_DESC",
        )

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="credits",
    )
    def credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 动画的日语配音演员。

        :return: AniList 人物边列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                characters(page: $page, perPage: $count, sort: [ROLE, RELEVANCE]) {
                  edges {
                    role
                    node { id name { full native } }
                    voiceActors(language: JAPANESE, sort: [RELEVANCE]) {
                      id name { full native alternative } image { large medium } siteUrl
                    }
                  }
                }
              }
            }
        """
        result = self._invoke(query, {"id": anilist_id, "page": page, "count": count})
        return result.get("Media", {}).get("characters", {}).get("edges") or [] if result else []

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="credits",
    )
    async def async_credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 动画的日语配音演员。

        :return: AniList 人物边列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                characters(page: $page, perPage: $count, sort: [ROLE, RELEVANCE]) {
                  edges {
                    role
                    node { id name { full native } }
                    voiceActors(language: JAPANESE, sort: [RELEVANCE]) {
                      id name { full native alternative } image { large medium } siteUrl
                    }
                  }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": anilist_id, "page": page, "count": count})
        return result.get("Media", {}).get("characters", {}).get("edges") or [] if result else []

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="recommendations",
    )
    def recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 动画相关推荐。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                recommendations(page: $page, perPage: $count, sort: [RATING_DESC, ID]) {
                  nodes { mediaRecommendation { id } }
                }
              }
            }
        """
        result = self._invoke(query, {"id": anilist_id, "page": page, "count": count})
        nodes = result.get("Media", {}).get("recommendations", {}).get("nodes") or [] if result else []
        media_ids = [node.get("mediaRecommendation", {}).get("id") for node in nodes]
        return self._medias_by_ids(media_ids)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="recommendations",
    )
    async def async_recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 动画相关推荐。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Media(id: $id, type: ANIME) {
                recommendations(page: $page, perPage: $count, sort: [RATING_DESC, ID]) {
                  nodes { mediaRecommendation { id } }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": anilist_id, "page": page, "count": count})
        nodes = result.get("Media", {}).get("recommendations", {}).get("nodes") or [] if result else []
        media_ids = [node.get("mediaRecommendation", {}).get("id") for node in nodes]
        return await self._async_medias_by_ids(media_ids)

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_detail",
    )
    def person_detail(self, person_id: int) -> Optional[dict]:
        """
        获取 AniList 演员详情。

        :param person_id: AniList 人物 ID
        :return: AniList 人物详情
        """
        query = """
            query ($id: Int!) {
              Staff(id: $id) {
                id name { full native alternative } image { large medium }
                description(asHtml: false) dateOfBirth { year month day }
                dateOfDeath { year month day } gender homeTown primaryOccupations siteUrl
              }
            }
        """
        result = self._invoke(query, {"id": person_id})
        return result.get("Staff") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_detail",
    )
    async def async_person_detail(self, person_id: int) -> Optional[dict]:
        """
        异步获取 AniList 演员详情。

        :param person_id: AniList 人物 ID
        :return: AniList 人物详情
        """
        query = """
            query ($id: Int!) {
              Staff(id: $id) {
                id name { full native alternative } image { large medium }
                description(asHtml: false) dateOfBirth { year month day }
                dateOfDeath { year month day } gender homeTown primaryOccupations siteUrl
              }
            }
        """
        result = await self._async_invoke(query, {"id": person_id})
        return result.get("Staff") if result else None

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_credits",
    )
    def person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        获取 AniList 演员参与的动画作品。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Staff(id: $id) {
                characterMedia(page: $page, perPage: $count, sort: [POPULARITY_DESC]) {
                  nodes { id }
                }
              }
            }
        """
        result = self._invoke(query, {"id": person_id, "page": page, "count": count})
        nodes = result.get("Staff", {}).get("characterMedia", {}).get("nodes") or [] if result else []
        return self._medias_by_ids([node.get("id") for node in nodes])

    @cached(
        maxsize=settings.CONF.anilist,
        ttl=settings.CONF.meta,
        skip_empty=True,
        shared_key="person_credits",
    )
    async def async_person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict]:
        """
        异步获取 AniList 演员参与的动画作品。

        :return: AniList 媒体列表
        """
        query = """
            query ($id: Int!, $page: Int!, $count: Int!) {
              Staff(id: $id) {
                characterMedia(page: $page, perPage: $count, sort: [POPULARITY_DESC]) {
                  nodes { id }
                }
              }
            }
        """
        result = await self._async_invoke(query, {"id": person_id, "page": page, "count": count})
        nodes = result.get("Staff", {}).get("characterMedia", {}).get("nodes") or [] if result else []
        return await self._async_medias_by_ids([node.get("id") for node in nodes])

    def clear_cache(self) -> None:
        """清理 AniList 接口缓存"""
        for method in (
            self.detail,
            self.search,
            self.discover,
            self.credits,
            self.recommendations,
            self.person_detail,
            self.person_credits,
        ):
            method.cache_clear()
