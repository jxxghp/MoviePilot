"""
AniList GraphQL 客户端，统一查询计划、中文标题补充及同步异步缓存。
"""

import json
from dataclasses import dataclass
from datetime import date
from math import isfinite
from time import time
from typing import Any, Callable, Optional, Protocol, TypeVar, cast

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.foundation.url import UrlUtils
from app.runtime.cache import AsyncFileCache, FileCache, async_cache_mutation, cache_mutation, cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

_Result = TypeVar("_Result")
_RequestFunction = TypeVar("_RequestFunction", bound=Callable[..., object])


class _GraphQLResponse(Protocol):
    """
    客户端响应在 GraphQL 解码阶段使用的最小接口。
    """

    @property
    def status_code(self) -> int:
        """
        读取已经取得的 HTTP 响应状态码。

        :return: 原始 HTTP 状态码，调用方仅接受 200
        """
        ...

    def json(self) -> object:
        """
        解码响应正文，不负责校验 GraphQL 业务结构。

        解码失败可抛出解析异常，由 GraphQL 提取层统一记录并返回空结果。

        :return: JSON 解码后的原始值，调用方继续检查映射和 data 字段
        """
        ...


class _ClearableRequest(Protocol):
    """
    缓存装饰器附加的同步清理入口。
    """

    def cache_clear(self) -> None:
        """
        同步清空此查询使用的缓存命名空间。

        同步与异步查询共享缓存身份时，两者后续调用均需重新获取结果。
        """
        ...


def _cached_request(shared_key: str) -> Callable[[_RequestFunction], _RequestFunction]:
    """
    保留查询签名，并沿用同一同步异步缓存命名空间和过期策略。
    """

    def decorate(function: _RequestFunction) -> _RequestFunction:
        """
        缓存查询结果而不转换返回载荷。
        """
        return cached(
            maxsize=get_runtime_setting('CONF').anilist,
            ttl=get_runtime_setting('CONF').meta,
            skip_empty=True,
            shared_key=shared_key,
        )(function)

    return decorate


@dataclass(frozen=True, slots=True)
class _AniListRequestPlan:
    """
    同步与异步传输共用的 GraphQL 查询计划。

    :param query: 包含所需字段和变量声明的 GraphQL 查询文本
    :param variables: 变量名到业务值的独立映射，不修改调用方传入的映射
    """

    query: str
    variables: dict[str, Any]


class AniListApi:
    """
    通过官方 GraphQL 接口查询 AniList，并补充本地缓存的中文标题。

    中文数据集每天更新，下载失败时保留旧数据并在五分钟后重试。
    """

    _official_url = "https://graphql.anilist.co"
    _translations_url = (
        "https://raw.githubusercontent.com/soruly/anilist-chinese/"
        "master/anilist-chinese.json"
    )
    # 上游中文数据集每天更新，磁盘和内存沿用同一有效期（秒）。
    _translations_ttl = 24 * 60 * 60
    _translations_retry = 5 * 60
    _translations_region = "anilist"
    _translations_key = "anilist-chinese.json"
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
    _search_query = f"""
        query ($search: String!, $count: Int!) {{
          Page(page: 1, perPage: $count) {{
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{ {_media_fields} }}
          }}
        }}
    """
    _credits_query = """
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
    _recommendations_query = """
        query ($id: Int!, $page: Int!, $count: Int!) {
          Media(id: $id, type: ANIME) {
            recommendations(page: $page, perPage: $count, sort: [RATING_DESC, ID]) {
              nodes { mediaRecommendation { id } }
            }
          }
        }
    """
    _person_detail_query = """
        query ($id: Int!) {
          Staff(id: $id) {
            id name { full native alternative } image { large medium }
            description(asHtml: false) dateOfBirth { year month day }
            dateOfDeath { year month day } gender homeTown primaryOccupations siteUrl
          }
        }
    """
    _person_credits_query = """
        query ($id: Int!, $page: Int!, $count: Int!) {
          Staff(id: $id) {
            characterMedia(page: $page, perPage: $count, sort: [POPULARITY_DESC]) {
              nodes { id }
            }
          }
        }
    """

    def __init__(self) -> None:
        """
        初始化同步与异步请求客户端。
        """
        headers = {
            "User-Agent": get_runtime_setting('NORMAL_USER_AGENT'),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._request = RequestUtils(
            proxies=get_runtime_setting('PROXY'),
            headers=headers,
        )
        self._async_request = AsyncRequestUtils(
            proxies=get_runtime_setting('PROXY'),
            headers=headers,
        )
        self._translations: Optional[dict[object, dict[str, Any]]] = None
        self._translations_expires_at = 0.0
        self._file_cache = None
        self._async_file_cache = None
        try:
            cache_path = get_runtime_setting('CACHE_PATH')
            self._file_cache = FileCache(base=cache_path, local_only=True)
            self._async_file_cache = AsyncFileCache(base=cache_path, local_only=True)
        except OSError as err:
            logger.warning(f"初始化 anilist-chinese 磁盘缓存失败：{err}")

    @staticmethod
    def _extract_response(response: Optional[_GraphQLResponse]) -> Optional[dict[str, Any]]:
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
        if not isinstance(result, dict):
            return None
        if result.get("errors"):
            logger.warning(f"AniList 接口返回错误：{result.get('errors')}")
            return None
        data = result.get("data")
        return data if isinstance(data, dict) else None

    @staticmethod
    def _request_plan(
            query: str, variables: dict[str, Any]
    ) -> _AniListRequestPlan:
        """
        构造同步与异步 GraphQL 传输共同使用的请求计划。
        """
        return _AniListRequestPlan(query=query, variables=dict(variables))

    @classmethod
    def _project_translated(
            cls,
            result: Optional[dict[str, Any]],
            translations: dict[object, dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """
        把成功响应与中文数据集按同一规则合并。
        """
        return cls._inject_chinese(result, translations) if result else result

    def _invoke(self, query: str, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        请求 GraphQL 并合并中文标题数据。

        :param query: GraphQL 查询文本
        :param variables: 本次查询的变量映射
        :return: 经中文字段补充的 data 映射；请求或 GraphQL 业务失败时为 None
        """
        plan = self._request_plan(query, variables)
        payload = {"query": plan.query, "variables": plan.variables}
        response = self._request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        if not result:
            return result
        return self._project_translated(result, self._translation_map())

    async def _async_invoke(self, query: str, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        异步请求 GraphQL 并合并中文标题数据。

        :param query: GraphQL 查询文本
        :param variables: 本次查询的变量映射
        :return: 经中文字段补充的 data 映射；请求或 GraphQL 业务失败时为 None
        """
        plan = self._request_plan(query, variables)
        payload = {"query": plan.query, "variables": plan.variables}
        response = await self._async_request.post_res(self._official_url, json=payload)
        result = self._extract_response(response)
        if not result:
            return result
        translations = await self._async_translation_map()
        return self._project_translated(result, translations)

    @staticmethod
    def _build_translation_map(items: object) -> dict[object, dict[str, Any]]:
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
            if isinstance(item, dict)
            and isinstance(item.get("id"), (int, str)) and item.get("id")
            and isinstance(item.get("title"), str)
            and isinstance(item.get("synonyms", []), list)
            and all(isinstance(alias, str) for alias in item.get("synonyms", []))
        }

    def _translation_routes(self) -> list[tuple[str, bool]]:
        """
        返回中文数据集的下载地址及直连标志，GitHub 镜像优先且不叠加部署代理。

        :return: 镜像和原地址的有序列表，原地址沿用 PROXY
        """
        mirror = str(get_runtime_setting('GITHUB_PROXY') or '').strip()
        routes = [(self._translations_url, False)]
        if mirror:
            routes.insert(0, (f"{UrlUtils.standardize_base_url(mirror)}{self._translations_url}", True))
        return routes

    @classmethod
    def _decode_translation_cache(cls, content: Optional[bytes]) -> tuple[dict[object, dict[str, Any]], float]:
        """
        校验磁盘快照并读取数据和绝对过期时间，损坏快照按未命中处理。
        """
        if content:
            try:
                payload = json.loads(content)
                if isinstance(payload, dict):
                    expires_at = payload.get("expires_at")
                    if isinstance(expires_at, (int, float)) and isfinite(expires_at):
                        return cls._build_translation_map(payload.get("items")), float(expires_at)
            except (ValueError, TypeError) as err:
                logger.warning(f"解析 anilist-chinese 磁盘缓存失败：{err}")
        return {}, 0.0

    def _translation_snapshot(self, items: object) -> Optional[bytes]:
        """
        接收有效下载并生成磁盘快照；无效载荷不得覆盖内存或旧快照。
        """
        translations = self._build_translation_map(items)
        if not translations:
            return None
        self._translations = translations
        self._translations_expires_at = time() + self._translations_ttl
        return json.dumps({
            "expires_at": self._translations_expires_at,
            "items": list(translations.values()),
        }, ensure_ascii=False).encode('utf-8')

    def _reuse_translation_cache(self, translations: dict[object, dict[str, Any]], expires_at: float) -> bool:
        """
        恢复磁盘缓存；内存仍有效时优先复用，过期数据仅保留为下载失败的回退。
        """
        if self._translations is not None and time() < self._translations_expires_at:
            return True
        if translations and (not self._translations or expires_at > self._translations_expires_at):
            self._translations = translations
            self._translations_expires_at = expires_at
            return time() < expires_at
        return False

    def _translation_fallback(self) -> dict[object, dict[str, Any]]:
        """
        下载失败时保留旧中文数据，短暂退避后允许重试，不把空结果永久缓存。
        """
        logger.warning("加载 anilist-chinese 中文数据集失败，暂用已有数据，五分钟后重试")
        self._translations = self._translations or {}
        self._translations_expires_at = time() + self._translations_retry
        return self._translations

    def _translation_map(self) -> dict[object, dict[str, Any]]:
        """
        同步复用中文数据快照，仅在缺失或过期时下载；并发调用共用缓存写锁。

        :return: 中文标题数据索引
        """
        with cache_mutation("anilist-translations"):
            if self._reuse_translation_cache({}, 0.0):
                return self._translations or {}
            content = None
            try:
                if self._file_cache is not None:
                    content = self._file_cache.get(self._translations_key, region=self._translations_region)
            except OSError as err:
                logger.warning(f"读取 anilist-chinese 磁盘缓存失败：{err}")
            if self._reuse_translation_cache(*self._decode_translation_cache(content)):
                return self._translations or {}
            for url, direct in self._translation_routes():
                request = RequestUtils(ua=get_runtime_setting('NORMAL_USER_AGENT')) if direct else self._request
                snapshot = self._translation_snapshot(request.get_json(url))
                if snapshot is None:
                    continue
                try:
                    if self._file_cache is not None:
                        self._file_cache.set(self._translations_key, snapshot, region=self._translations_region)
                except OSError as err:
                    logger.warning(f"写入 anilist-chinese 磁盘缓存失败：{err}")
                return self._translations or {}
            return self._translation_fallback()

    async def _async_translation_map(self) -> dict[object, dict[str, Any]]:
        """
        异步复用同一磁盘快照和写锁，文件读写不阻塞事件循环。

        :return: 中文标题数据索引
        """
        async with async_cache_mutation("anilist-translations"):
            if self._reuse_translation_cache({}, 0.0):
                return self._translations or {}
            content = None
            try:
                if self._async_file_cache is not None:
                    content = await self._async_file_cache.get(self._translations_key, region=self._translations_region)
            except OSError as err:
                logger.warning(f"读取 anilist-chinese 磁盘缓存失败：{err}")
            if self._reuse_translation_cache(*self._decode_translation_cache(content)):
                return self._translations or {}
            for url, direct in self._translation_routes():
                request = AsyncRequestUtils(ua=get_runtime_setting('NORMAL_USER_AGENT')) if direct else self._async_request
                snapshot = self._translation_snapshot(await request.get_json(url))
                if snapshot is None:
                    continue
                try:
                    if self._async_file_cache is not None:
                        await self._async_file_cache.set(
                            self._translations_key, snapshot, region=self._translations_region,
                        )
                except OSError as err:
                    logger.warning(f"写入 anilist-chinese 磁盘缓存失败：{err}")
                return self._translations or {}
            return self._translation_fallback()

    @classmethod
    def _inject_chinese(cls, value: _Result, translations: dict[object, dict[str, Any]]) -> _Result:
        """
        递归把中文标题与别名合并到媒体响应中。

        :param value: 待处理的嵌套响应，列表和映射会原地修改
        :param translations: 以 AniList 标识索引的中文标题数据
        :return: 传入的对象本身，非容器值保持不变
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
    ) -> dict[str, Any]:
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
    def _page_medias(result: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        从分页响应中提取媒体列表。

        :param result: GraphQL data 字段
        :return: AniList 媒体列表
        """
        return result.get("Page", {}).get("media") or [] if result else []

    @staticmethod
    def _ordered_medias(media_ids: list[int], medias: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        按上游关系顺序重排批量查询返回的媒体。

        :param media_ids: 关系查询返回的 AniList 媒体 ID
        :param medias: Page.media 批量查询结果
        :return: 保持原关系顺序的媒体列表
        """
        media_map = {media.get("id"): media for media in medias if media.get("id")}
        return [media_map[media_id] for media_id in media_ids if media_id in media_map]

    @staticmethod
    def _media_id_plan(
            media_ids: list[int]
    ) -> tuple[list[int], dict[str, Any]]:
        """
        去重批量媒体 ID，并构造根级 Page 查询变量。
        """
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        return unique_ids, {"ids": unique_ids, "count": len(unique_ids)}

    @staticmethod
    def _field(
            result: Optional[dict[str, Any]], name: str
    ) -> Optional[dict[str, Any]]:
        """
        从 GraphQL data 中提取一个根字段。
        """
        return result.get(name) if result else None

    @staticmethod
    def _credits_edges(
            result: Optional[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        从动画人物响应中提取配音关系边。
        """
        if not result:
            return []
        return result.get("Media", {}).get("characters", {}).get("edges") or []

    @staticmethod
    def _recommendation_ids(
            result: Optional[dict[str, Any]]
    ) -> list[int]:
        """
        按推荐关系顺序提取有效 AniList 媒体 ID。
        """
        if not result:
            return []
        nodes = result.get("Media", {}).get("recommendations", {}).get("nodes") or []
        return [
            media_id
            for node in nodes
            if isinstance(node, dict)
            if isinstance(
                (media_id := (node.get("mediaRecommendation") or {}).get("id")),
                int,
            )
        ]

    @staticmethod
    def _person_media_ids(
            result: Optional[dict[str, Any]]
    ) -> list[int]:
        """
        按人物作品关系顺序提取有效 AniList 媒体 ID。
        """
        if not result:
            return []
        nodes = result.get("Staff", {}).get("characterMedia", {}).get("nodes") or []
        return [
            media_id
            for node in nodes
            if isinstance(node, dict)
            if isinstance((media_id := node.get("id")), int)
        ]

    @staticmethod
    def _trend_filters(page: int, count: int) -> dict[str, Any]:
        """
        构造当前趋势榜共同使用的探索参数。
        """
        return {"page": page, "count": count, "sort": "TRENDING_DESC"}

    @classmethod
    def _season_filters(cls, page: int, count: int) -> dict[str, Any]:
        """
        构造当前季度热门榜共同使用的探索参数。
        """
        season, season_year = cls._current_season()
        return {
            "page": page,
            "count": count,
            "season": season,
            "season_year": season_year,
            "sort": "POPULARITY_DESC",
        }

    def _medias_by_ids(self, media_ids: list[int]) -> list[dict[str, Any]]:
        """
        通过根级 Page.media 批量查询媒体并补充中文标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids, variables = self._media_id_plan(media_ids)
        if not unique_ids:
            return []
        result = self._invoke(
            self._media_by_ids_query,
            variables,
        )
        return self._ordered_medias(media_ids, self._page_medias(result))

    async def _async_medias_by_ids(self, media_ids: list[int]) -> list[dict[str, Any]]:
        """
        异步通过根级 Page.media 批量查询媒体并补充中文标题。

        :param media_ids: AniList 媒体 ID 列表
        :return: 按输入顺序排列的媒体列表
        """
        unique_ids, variables = self._media_id_plan(media_ids)
        if not unique_ids:
            return []
        result = await self._async_invoke(
            self._media_by_ids_query,
            variables,
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

    @_cached_request("detail")
    def detail(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        读取 AniList 动画详情并补充中文字段。

        :param anilist_id: AniList 动画条目标识
        :return: 原始媒体映射；请求失败或详情缺失时为 None
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        return self._field(self._invoke(query, {"id": anilist_id}), "Media")

    @_cached_request("detail")
    async def async_detail(self, anilist_id: int) -> Optional[dict[str, Any]]:
        """
        异步读取 AniList 动画详情并补充中文字段。

        :param anilist_id: AniList 动画条目标识
        :return: 原始媒体映射；请求失败或详情缺失时为 None
        """
        query = f"query ($id: Int!) {{ Media(id: $id, type: ANIME) {{ {self._media_fields} }} }}"
        result = await self._async_invoke(query, {"id": anilist_id})
        return self._field(result, "Media")

    @_cached_request("search")
    def search(self, name: str, count: int = 20) -> list[dict[str, Any]]:
        """
        按标题查询动画候选。

        :param name: 用于全文匹配的动画标题
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；无匹配或请求失败时为空列表
        """
        result = self._invoke(
            self._search_query, {"search": name, "count": count}
        )
        return self._page_medias(result)

    @_cached_request("search")
    async def async_search(self, name: str, count: int = 20) -> list[dict[str, Any]]:
        """
        异步按标题查询动画候选。

        :param name: 用于全文匹配的动画标题
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；无匹配或请求失败时为空列表
        """
        result = await self._async_invoke(
            self._search_query, {"search": name, "count": count}
        )
        return self._page_medias(result)

    @_cached_request("discover")
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
    ) -> list[dict[str, Any]]:
        """
        按组合条件探索非成人动画。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :param search: 可选标题关键词
        :param genre: 可选题材名称，按 AniList 取值传入
        :param media_format: 可选发布格式，如 TV 或 MOVIE
        :param season: 可选季度枚举，如 WINTER 或 FALL
        :param season_year: 可选季度所属年份
        :param status: 可选播出状态
        :param country: 可选来源国家或地区代码
        :param sort: 可选排序枚举，空值使用 POPULARITY_DESC
        :return: 原始媒体列表；无匹配或请求失败时为空列表
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

    @_cached_request("discover")
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
    ) -> list[dict[str, Any]]:
        """
        异步按组合条件探索非成人动画。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :param search: 可选标题关键词
        :param genre: 可选题材名称，按 AniList 取值传入
        :param media_format: 可选发布格式，如 TV 或 MOVIE
        :param season: 可选季度枚举，如 WINTER 或 FALL
        :param season_year: 可选季度所属年份
        :param status: 可选播出状态
        :param country: 可选来源国家或地区代码
        :param sort: 可选排序枚举，空值使用 POPULARITY_DESC
        :return: 原始媒体列表；无匹配或请求失败时为空列表
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

    def trending(self, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        按趋势热度降序读取动画榜单。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；请求失败时为空列表
        """
        return self.discover(**self._trend_filters(page, count))

    async def async_trending(self, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        异步按趋势热度降序读取动画榜单。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；请求失败时为空列表
        """
        return await self.async_discover(**self._trend_filters(page, count))

    def popular_this_season(self, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        按本地当前日期确定季度，查询该季度人气榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；请求失败时为空列表
        """
        return self.discover(**self._season_filters(page, count))

    async def async_popular_this_season(self, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        异步按本地当前日期确定季度，查询该季度人气榜。

        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；请求失败时为空列表
        """
        return await self.async_discover(**self._season_filters(page, count))

    @_cached_request("credits")
    def credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        分页读取动画角色及其日语配音演员。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始角色关系列表；无结果或请求失败时为空列表
        """
        result = self._invoke(
            self._credits_query,
            {"id": anilist_id, "page": page, "count": count},
        )
        return self._credits_edges(result)

    @_cached_request("credits")
    async def async_credits(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        异步分页读取动画角色及其日语配音演员。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始角色关系列表；无结果或请求失败时为空列表
        """
        result = await self._async_invoke(
            self._credits_query,
            {"id": anilist_id, "page": page, "count": count},
        )
        return self._credits_edges(result)

    @_cached_request("recommendations")
    def recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        读取动画推荐关系，再批量补全媒体详情。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 按推荐关系顺序排列的媒体列表；无结果或请求失败时为空列表
        """
        result = self._invoke(
            self._recommendations_query,
            {"id": anilist_id, "page": page, "count": count},
        )
        return self._medias_by_ids(self._recommendation_ids(result))

    @_cached_request("recommendations")
    async def async_recommendations(self, anilist_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        异步读取动画推荐关系，再批量补全媒体详情。

        :param anilist_id: AniList 动画条目标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 按推荐关系顺序排列的媒体列表；无结果或请求失败时为空列表
        """
        result = await self._async_invoke(
            self._recommendations_query,
            {"id": anilist_id, "page": page, "count": count},
        )
        return await self._async_medias_by_ids(self._recommendation_ids(result))

    @_cached_request("person_detail")
    def person_detail(self, person_id: int) -> Optional[dict[str, Any]]:
        """
        读取 AniList 人物详情。

        :param person_id: AniList 人物标识
        :return: 原始 Staff 映射；请求失败或人物不存在时为 None
        """
        return self._field(
            self._invoke(self._person_detail_query, {"id": person_id}),
            "Staff",
        )

    @_cached_request("person_detail")
    async def async_person_detail(self, person_id: int) -> Optional[dict[str, Any]]:
        """
        异步读取 AniList 人物详情。

        :param person_id: AniList 人物标识
        :return: 原始 Staff 映射；请求失败或人物不存在时为 None
        """
        result = await self._async_invoke(
            self._person_detail_query, {"id": person_id}
        )
        return self._field(result, "Staff")

    @_cached_request("person_credits")
    def person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        查询人物配音作品，并按关系顺序补全媒体详情。

        :param person_id: AniList 人物标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；无结果或请求失败时为空列表
        """
        result = self._invoke(
            self._person_credits_query,
            {"id": person_id, "page": page, "count": count},
        )
        return self._medias_by_ids(self._person_media_ids(result))

    @_cached_request("person_credits")
    async def async_person_credits(self, person_id: int, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        """
        异步查询人物配音作品，并按关系顺序补全媒体详情。

        :param person_id: AniList 人物标识
        :param page: 从 1 开始的页码，原样传入服务端
        :param count: 每页条数，原样传入服务端，不在本地截断范围
        :return: 原始媒体列表；无结果或请求失败时为空列表
        """
        result = await self._async_invoke(
            self._person_credits_query,
            {"id": person_id, "page": page, "count": count},
        )
        return await self._async_medias_by_ids(self._person_media_ids(result))

    def clear_cache(self) -> None:
        """
        清理 AniList 接口缓存。
        """
        for method in (
            self.detail,
            self.search,
            self.discover,
            self.credits,
            self.recommendations,
            self.person_detail,
            self.person_credits,
        ):
            cast(_ClearableRequest, method).cache_clear()
