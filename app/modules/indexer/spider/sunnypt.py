import base64
import json
import time
from typing import List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils


class SunnyPTSpider:
    """
    SunnyPT MoviePilot API 索引器
    """

    _size = 100
    _category_cache_ttl = 3600
    _category_cache = {}

    def __init__(self, indexer: dict):
        """
        初始化 SunnyPT API 索引器

        :param indexer: 合并站点认证信息后的索引配置
        """
        indexer = indexer or {}
        self._indexer_id = indexer.get("id")
        self._name = indexer.get("name") or "SunnyPT"
        self._site_url = indexer.get("domain") or "https://sunnypt.top/"
        self._api_url = str(
            indexer.get("api_url") or "https://api.sunnypt.top/api/v1/mp"
        ).rstrip("/")
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._api_key = indexer.get("apikey")
        self._timeout = indexer.get("timeout") or 15
        self._configured_categories = self._parse_configured_categories(
            indexer.get("category") or {}
        )

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量

        :param keyword: 搜索关键字，SunnyPT 不按关键字改变分页容量
        :return: 搜索接口单页容量
        """
        return cls._size

    @staticmethod
    def _parse_configured_categories(category_config: dict) -> dict:
        """
        从站点索引配置提取电影和电视剧分类 ID，作为分类接口不可用时的兜底

        :param category_config: Build 站点配置中的分类段
        :return: 按 API media_type 索引的分类 ID
        """
        category_map = {"movie": [], "tv": []}
        for media_type in category_map:
            for item in category_config.get(media_type) or []:
                category_id = item.get("id") if isinstance(item, dict) else item
                if category_id is not None:
                    category_map[media_type].append(str(category_id))
        return category_map

    @staticmethod
    def _parse_category_items(items: list) -> dict:
        """
        将分类接口结果转换为按媒体类型索引的分类 ID

        :param items: SunnyPT 分类接口 data 数组
        :return: 按 API media_type 索引的分类 ID
        """
        category_map = {"movie": [], "tv": []}
        for item in items or []:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            category_id = str(item["id"])
            for media_type in item.get("media_types") or []:
                if media_type in category_map and category_id not in category_map[media_type]:
                    category_map[media_type].append(category_id)
        return category_map

    @classmethod
    def _get_cached_categories(cls, api_url: str) -> Optional[dict]:
        """
        获取未过期的站点分类缓存

        :param api_url: SunnyPT API Base URL
        :return: 分类映射，缓存不存在或过期时返回 None
        """
        cached = cls._category_cache.get(api_url)
        if not cached:
            return None
        cached_at, category_map = cached
        if time.monotonic() - cached_at >= cls._category_cache_ttl:
            cls._category_cache.pop(api_url, None)
            return None
        return category_map

    @classmethod
    def _set_cached_categories(cls, api_url: str, category_map: dict) -> None:
        """
        缓存站点分类映射

        :param api_url: SunnyPT API Base URL
        :param category_map: 按媒体类型索引的分类 ID
        """
        cls._category_cache[api_url] = (time.monotonic(), category_map)

    def _request_headers(self) -> dict:
        """
        构造 SunnyPT API 请求头

        :return: 不包含 Cookie 和 Authorization 的 API Key 请求头
        """
        return {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
            "X-API-Key": self._api_key,
        }

    def _response_data(self, response, operation: str):
        """
        校验 SunnyPT 通用响应并返回 data 字段

        :param response: RequestUtils 返回的响应对象
        :param operation: 日志中使用的操作名称
        :return: 接口成功时返回 data，失败时返回 None
        """
        if response is None:
            logger.warning(f"{self._name} {operation}失败，无法连接 API 服务")
            return None
        if response.status_code != 200:
            logger.warning(f"{self._name} {operation}失败，HTTP 错误码：{response.status_code}")
            return None
        try:
            payload = response.json() or {}
        except (TypeError, ValueError) as err:
            logger.warning(f"{self._name} {operation}响应不是有效 JSON：{str(err)}")
            return None
        if not isinstance(payload, dict):
            logger.warning(f"{self._name} {operation}响应结构无效")
            return None
        if str(payload.get("code")) != "0":
            logger.warning(f"{self._name} {operation}失败：{payload.get('msg') or '未知错误'}")
            return None
        return payload.get("data")

    def _load_categories(self) -> dict:
        """
        同步读取并缓存 SunnyPT 分类映射，接口失败时使用 Build 配置兜底

        :return: 按媒体类型索引的分类 ID
        """
        cached = self._get_cached_categories(self._api_url)
        if cached is not None:
            return cached
        response = RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=f"{self._api_url}/categories")
        items = self._response_data(response, "获取分类")
        category_map = self._parse_category_items(items) if isinstance(items, list) else {}
        if not category_map or not any(category_map.values()):
            return self._configured_categories
        self._set_cached_categories(self._api_url, category_map)
        return category_map

    async def _async_load_categories(self) -> dict:
        """
        异步读取并缓存 SunnyPT 分类映射，接口失败时使用 Build 配置兜底

        :return: 按媒体类型索引的分类 ID
        """
        cached = self._get_cached_categories(self._api_url)
        if cached is not None:
            return cached
        response = await AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=f"{self._api_url}/categories")
        items = self._response_data(response, "获取分类")
        category_map = self._parse_category_items(items) if isinstance(items, list) else {}
        if not category_map or not any(category_map.values()):
            return self._configured_categories
        self._set_cached_categories(self._api_url, category_map)
        return category_map

    @staticmethod
    def _media_type_value(media_type: MediaType) -> Optional[str]:
        """
        将 MoviePilot 媒体类型转换为 SunnyPT API 枚举

        :param media_type: MoviePilot 媒体类型
        :return: movie、tv 或 None
        """
        if media_type == MediaType.MOVIE:
            return "movie"
        if media_type == MediaType.TV:
            return "tv"
        return None

    def _build_params(
        self,
        keyword: Optional[str],
        media_type: MediaType,
        category: Optional[str],
        page: Optional[int],
        category_map: dict,
    ) -> dict:
        """
        构造 SunnyPT 种子搜索参数

        :param keyword: 搜索关键字或完整 IMDb ID
        :param media_type: MoviePilot 媒体类型
        :param category: 用户显式选择的分类 ID
        :param page: MoviePilot 从 0 开始的页码
        :param category_map: SunnyPT 分类接口返回的映射
        :return: SunnyPT API 查询参数
        """
        params = {
            "page": int(page or 0) + 1,
            "page_size": self._size,
            "sort": "created_at",
            "order": "desc",
        }
        if keyword:
            params["keyword"] = keyword
        api_media_type = self._media_type_value(media_type)
        if api_media_type:
            params["media_type"] = api_media_type
        categories = category
        if not categories and api_media_type:
            categories = ",".join(category_map.get(api_media_type) or [])
        if categories:
            params["categories"] = categories
        return params

    def _parse_result(self, results: List[dict]) -> List[dict]:
        """
        将 SunnyPT API 种子数据转换为 MoviePilot 标准字段

        :param results: SunnyPT 种子接口 data.items 数组
        :return: MoviePilot 标准种子字典列表
        """
        torrents = []
        for result in results or []:
            if not isinstance(result, dict):
                continue
            media_type = result.get("media_type")
            if media_type == "movie":
                category = MediaType.MOVIE.value
            elif media_type == "tv":
                category = MediaType.TV.value
            else:
                category = MediaType.UNKNOWN.value

            promotion = result.get("promotion") or {}
            promotion_active = bool(promotion.get("is_active"))
            download_factor = float(promotion.get("down_multiplier", 1.0)) \
                if promotion_active else 1.0
            upload_factor = float(promotion.get("up_multiplier", 1.0)) \
                if promotion_active else 1.0
            freedate = StringUtils.unify_datetime_str(promotion.get("until")) \
                if promotion_active and promotion.get("until") else None
            torrent_id = result.get("id")
            torrents.append({
                "title": result.get("title"),
                "description": result.get("subtitle"),
                "enclosure": self._build_download_url(torrent_id),
                "pubdate": StringUtils.unify_datetime_str(result.get("created_at")),
                "size": int(result.get("size") or 0),
                "seeders": int(result.get("seeders") or 0),
                "peers": int(result.get("leechers") or 0),
                "grabs": int(result.get("completed") or 0),
                "downloadvolumefactor": download_factor,
                "uploadvolumefactor": upload_factor,
                "freedate": freedate,
                "page_url": result.get("details_url"),
                "imdbid": result.get("imdb_id"),
                "labels": result.get("tags") or [],
                "hit_and_run": bool(result.get("hit_and_run")),
                "category": category,
            })
        return torrents

    def _process_search_response(self, response) -> Tuple[bool, List[dict]]:
        """
        处理 SunnyPT 种子搜索响应

        :param response: RequestUtils 返回的响应对象
        :return: 是否失败及标准种子列表
        """
        data = self._response_data(response, "搜索")
        if not isinstance(data, dict):
            return True, []
        return False, self._parse_result(data.get("items") or [])

    def search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        同步搜索 SunnyPT 种子

        :param keyword: 搜索关键字或完整 IMDb ID
        :param mtype: MoviePilot 媒体类型
        :param cat: 用户显式选择的分类 ID
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key")
            return True, []
        category_map = self._load_categories() if mtype and not cat else self._configured_categories
        params = self._build_params(keyword, mtype, cat, page, category_map)
        response = RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=f"{self._api_url}/torrents", params=params)
        return self._process_search_response(response)

    async def async_search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索 SunnyPT 种子

        :param keyword: 搜索关键字或完整 IMDb ID
        :param mtype: MoviePilot 媒体类型
        :param cat: 用户显式选择的分类 ID
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API Key")
            return True, []
        category_map = await self._async_load_categories() \
            if mtype and not cat else self._configured_categories
        params = self._build_params(keyword, mtype, cat, page, category_map)
        response = await AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).get_res(url=f"{self._api_url}/torrents", params=params)
        return self._process_search_response(response)

    def _build_download_url(self, torrent_id: int) -> str:
        """
        构造先换取短时下载地址再下载种子文件的两段式链接

        :param torrent_id: SunnyPT 种子 ID
        :return: Base64 请求配置与 download-token 接口 URL
        """
        request_config = {
            "method": "post",
            "cookie": False,
            "header": {
                "X-API-Key": self._api_key,
                "Accept": "application/json",
            },
            "proxy": self._use_proxy,
            "result": "data.download_url",
            "result_base_url": self._api_url,
        }
        encoded_config = base64.b64encode(
            json.dumps(request_config).encode("utf-8")
        ).decode("ascii")
        token_url = f"{self._api_url}/torrents/{torrent_id}/download-token"
        return f"[{encoded_config}]{token_url}"
