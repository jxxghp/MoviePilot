"""
通过高清视界 REST API 搜索和浏览种子，不依赖 Cookie 或 HTML 登录标记。
"""

import re
from typing import Any, Optional
from urllib.parse import urljoin

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.site.observation import report_site_search_page
from app.application.site.throttle import async_wait_for_site_request, defer_site_request, wait_for_site_request
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.foundation import temporal as time_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType


class HDAreaSpider:
    """
    高清视界 API Key 索引器，搜索和空关键词最新列表共用零起始分页。
    """

    # 官方搜索接口单页最多返回 100 条。
    _size = 100

    def __init__(self, indexer: dict[str, Any]) -> None:
        """
        保存站点认证、分类和流控配置，网页 Cookie 不参与 API 请求。

        :param indexer: 合并当前用户配置的站点资源
        """
        self._name = indexer.get("name") or "高清视界"
        self._site_url = str(indexer.get("domain") or "https://hdarea.club/").rstrip("/")
        self._api_url = str(indexer.get("api_url") or f"{self._site_url}/api/v1").rstrip("/")
        self._apikey = str(indexer.get("apikey") or "").strip()
        self._ua = indexer.get("ua") or get_runtime_setting("USER_AGENT")
        self._proxy = get_runtime_setting("PROXY") if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._timeout = indexer.get("timeout") or 15
        category = indexer.get("category") or {}
        self._categories = {
            media_type: [str(item["id"]) for item in category.get(key) or []]
            for media_type, key in ((MediaType.MOVIE, "movie"), (MediaType.TV, "tv"), (MediaType.MUSIC, "music"))
        }
        self._rate_site = {key: indexer.get(key) for key in
                           ("id", "domain", "limit_seconds", "limit_interval", "limit_count")}
        self.error_detail: Optional[str] = None

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        返回搜索与最新种子列表的固定分页容量。

        :param keyword: 保留统一入口签名，不影响单页容量
        :return: 每页最多 100 条
        """
        return cls._size

    def _headers(self) -> dict[str, str]:
        """
        构造仅使用 API Key 的 JSON 请求头。

        :return: 不含 Cookie 的独立请求头字典
        """
        return {"X-API-Key": self._apikey, "Accept": "application/json", "User-Agent": self._ua}

    def _params(self, keyword: Optional[str], mtype: Optional[MediaType], cat: Optional[str],
                page: Optional[int]) -> dict[str, Any]:
        """
        构造单次全局分页查询，显式分类优先，IMDb 使用精确筛选参数。

        :param keyword: 普通关键词或完整 IMDb ID；空值浏览最新种子
        :param mtype: 未指定分类时选择资源中的媒体分类
        :param cat: 逗号分隔的站点分类 ID
        :param page: 从 0 开始的页码
        :return: REST API 查询参数，不包含密钥
        """
        params: dict[str, Any] = {"page": int(page or 0), "per_page": self._size, "sort": "added", "order": "desc"}
        keyword = str(keyword or "").strip()
        if keyword:
            params["imdb" if re.fullmatch(r"tt\d+", keyword) else "keyword"] = keyword
        categories = cat
        if not categories and mtype in (MediaType.MOVIE, MediaType.TV, MediaType.MUSIC):
            categories = ",".join(self._categories.get(mtype) or [])
        if categories:
            params["cat"] = categories
        return params

    def get_download_url(self, torrent_id: object) -> str:
        """
        使用当前密钥按站内 ID 重建换票配方，不重新搜索或预先签发票据。

        :param torrent_id: 正整数站内种子 ID
        :return: 下载时才调用 genDlToken 的配方
        """
        return site_rules.hdarea_download_enclosure(
            self._api_url, self._site_url, torrent_id, self._apikey, self._use_proxy, self._ua,
        )

    def _parse_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """
        将 API 种子字段投影为 MoviePilot 资源，直接使用服务端当前有效的促销倍率。

        :param item: data.items 中的一条种子记录
        :return: 标准种子字段；共享或未知分类保持未知，不靠 Torznab 聚合号猜测媒体类型
        :raises ValueError: 种子身份、标题、计数或倍率无效
        """
        torrent_id = site_rules.hdarea_integer(item.get("id"), minimum=1)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("HDArea API 种子标题无效")
        download_factor = (
            site_rules.hdarea_number(item["download_volume_factor"])
            if item.get("download_volume_factor") is not None else None
        )
        upload_factor = (
            site_rules.hdarea_number(item["upload_volume_factor"])
            if item.get("upload_volume_factor") is not None else None
        )
        promotion_active = download_factor not in (None, 1) or upload_factor not in (None, 1)
        matching_types = [media_type for media_type, categories in self._categories.items()
                          if str(item.get("category_id")) in categories]
        category = matching_types[0] if len(matching_types) == 1 else MediaType.UNKNOWN
        result = {
            "title": item["name"], "description": item.get("small_descr"),
            "enclosure": self.get_download_url(torrent_id),
            "page_url": urljoin(f"{self._site_url}/", item.get("detail_url") or f"details.php?id={torrent_id}"),
            "pubdate": time_tools.normalize_datetime(item.get("added")),
            "size": site_rules.hdarea_integer(item.get("size")),
            "seeders": site_rules.hdarea_integer(item.get("seeders", 0)),
            "peers": site_rules.hdarea_integer(item.get("leechers", 0)),
            "grabs": site_rules.hdarea_integer(item.get("times_completed", 0)),
            "downloadvolumefactor": download_factor, "uploadvolumefactor": upload_factor,
            "freedate": time_tools.normalize_datetime(item.get("promotion_until")) if promotion_active else None,
            "category": category.value,
            "labels": item.get("tags") or [],
        }
        if re.fullmatch(r"tt\d+", str(item.get("imdb_id") or "")):
            result.update(media_source=MediaSource.IMDb, media_id=item["imdb_id"])
        elif re.fullmatch(r"[1-9]\d*", str(item.get("douban_id") or "")):
            result.update(media_source=MediaSource.Douban, media_id=str(item["douban_id"]))
        return result

    def _process_response(self, response: Any, page: int) -> tuple[bool, list[dict[str, Any]]]:
        """
        校验 API 及分页事实，错误不能被当成成功空列表，限流传播到共享站点冷却。

        :param response: 同步或异步传输响应，None 表示连接失败
        :param page: 本次请求的零起始页码
        :return: 是否失败及当前页资源
        :raises RateLimitExceededException: 远端已明确要求冷却
        """
        try:
            if response is None:
                raise ValueError("无法连接 HDArea API")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            data = site_rules.hdarea_response_data(response.status_code, payload, self._apikey)
            items, pagination = data.get("items"), data.get("pagination")
            if not isinstance(items, list) or not isinstance(pagination, dict):
                raise ValueError("HDArea API 种子分页结构无效")
            if site_rules.hdarea_integer(pagination.get("page")) != page:
                raise ValueError("HDArea API 返回了错误页码")
            if not all(isinstance(item, dict) for item in items):
                raise ValueError("HDArea API 种子条目无效")
            results = [self._parse_item(item) for item in items]
            has_more = pagination.get("has_more")
            report_site_search_page(raw_count=len(items), has_more=has_more if isinstance(has_more, bool) else None)
            self.error_detail = None
            return False, results
        except (TypeError, ValueError) as err:
            self.error_detail = str(err).replace(self._apikey, "[已隐藏]") if self._apikey else str(err)
            if torrent_rules.rate_limit_cooldown(self.error_detail) is not None:
                defer_site_request(self._rate_site, self.error_detail)
                raise RateLimitExceededException(self.error_detail) from err
            logger.warning(f"{self._name} 搜索失败：{self.error_detail}")
            return True, []

    def search(self, keyword: Optional[str], mtype: Optional[MediaType] = None, cat: Optional[str] = None,
               page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """
        同步搜索或浏览最新种子，不回退到 Cookie 页面。

        :param keyword: 普通关键词、完整 IMDb ID 或空值
        :param mtype: 未指定分类时使用的媒体类型
        :param cat: 显式分类 ID
        :param page: 零起始页码
        :return: 是否失败及当前页资源
        """
        if not self._apikey:
            self.error_detail = site_rules.HDAREA_KEY_REQUIRED
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        params = self._params(keyword, mtype, cat, page)
        wait_for_site_request(self._rate_site)
        response = RequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
            url=f"{self._api_url}/torrents", params=params, allow_redirects=False,
        )
        try:
            return self._process_response(response, params["page"])
        finally:
            if response is not None:
                response.close()

    async def async_search(self, keyword: Optional[str], mtype: Optional[MediaType] = None, cat: Optional[str] = None,
                           page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """
        异步执行相同的 API 查询及分页合同，取消等待不消耗提前预约的额度。

        :param keyword: 普通关键词、完整 IMDb ID 或空值
        :param mtype: 未指定分类时使用的媒体类型
        :param cat: 显式分类 ID
        :param page: 零起始页码
        :return: 是否失败及当前页资源
        """
        if not self._apikey:
            self.error_detail = site_rules.HDAREA_KEY_REQUIRED
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        params = self._params(keyword, mtype, cat, page)
        await async_wait_for_site_request(self._rate_site)
        response = await AsyncRequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
            url=f"{self._api_url}/torrents", params=params, allow_redirects=False,
        )
        try:
            return self._process_response(response, params["page"])
        finally:
            if response is not None:
                await response.aclose()
