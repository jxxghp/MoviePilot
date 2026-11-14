"""
通过高清视界 REST API 搜索和浏览种子，不依赖 Cookie 或 HTML 登录标记。
"""

import base64
import json
import math
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.site.observation import report_site_search_page
from app.application.site.throttle import async_wait_for_site_request, defer_site_request, wait_for_site_request
from app.domain import torrent as torrent_rules
from app.foundation import temporal as time_tools
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 读取、下载和签到分别需要 read、download 和 user 权限。
KEY_REQUIRED = "请在高清视界「控制面板 → 安全设定」获取 API Key，并填入站点 API Key"


def response_data(status_code: int, payload: object, apikey: str = "") -> dict[str, Any]:
    """
    校验 API 信封，HTTP、success 和业务码必须同时成功。

    :param status_code: 原始 HTTP 状态码
    :param payload: 已解码的 JSON；非 JSON 响应可以传入 None
    :param apikey: 本次密钥，仅用于移除错误消息中可能的凭据回显
    :return: 成功响应中的 data 对象
    :raises ValueError: 鉴权、权限、限流、业务失败或信封结构无效
    """
    envelope = payload if isinstance(payload, dict) else {}
    data = envelope.get("data")
    message = str(envelope.get("message") or "").strip()
    code = str(envelope.get("code"))
    if status_code == 401 or code == "401":
        message = "HDArea API Key 已过期或无效"
    elif status_code == 403 or code == "403":
        message = f"HDArea API Key 权限不足或 IP 不在白名单{f'：{message}' if message else ''}"
    elif status_code == 429 or code == "429":
        message = f"HDArea 站点限流：{message or 'HTTP 429，请稍后重试'}"
    elif status_code != 200:
        message = f"HDArea API 请求失败，HTTP {status_code}{f'：{message}' if message else ''}"
    elif envelope.get("success") is not True or code != "0":
        message = f"HDArea API 请求失败：{message or '响应结构或业务状态无效'}"
    elif isinstance(data, dict):
        return data
    else:
        message = "HDArea API 响应缺少有效 data"
    if apikey:
        message = message.replace(apikey, "[已隐藏]")
    raise ValueError(" ".join(message.split())[:300])


def integer(value: object, minimum: int = 0) -> int:
    """
    校验整数 ID、计数和字节字段，拒绝布尔值与浮点截断。

    :param value: JSON 整数或十进制整数字符串
    :param minimum: 允许的最小值，种子和用户 ID 使用 1
    :return: 已校验的整数
    :raises ValueError: 格式或范围无效；错误不回显账户字段
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise ValueError("HDArea API 整数字段无效")
    result = int(value)
    if result < minimum:
        raise ValueError("HDArea API 整数字段无效")
    return result


def number(value: object) -> float:
    """
    校验分享率、魔力和促销倍率，不接受负数或非有限值。

    :param value: JSON 数值或数值字符串
    :return: 有限的非负数值
    :raises ValueError: 类型或范围无效；错误不回显原值
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("HDArea API 数值字段无效")
    try:
        result = float(value)
    except (ValueError, OverflowError) as err:
        raise ValueError("HDArea API 数值字段无效") from err
    if not math.isfinite(result) or result < 0:
        raise ValueError("HDArea API 数值字段无效")
    return result


def download_enclosure(api_url: str, site_url: str, torrent_id: object, apikey: str,
                       proxy: bool = False, ua: Optional[str] = None) -> str:
    """
    构造下载时才签发短效 URL 的配方，不复用搜索时的过期票据。

    :param api_url: REST API 基础地址
    :param site_url: 网页来源，用于校验下载地址或拼接 dl_token
    :param torrent_id: 正整数站内种子 ID
    :param apikey: 具备 download 权限的 API Key
    :param proxy: 换票是否使用站点代理
    :param ua: 换票请求的 User-Agent
    :return: Base64 请求配置与 genDlToken 地址；缺少密钥时为空字符串
    :raises ValueError: 种子 ID 无效
    """
    if not apikey:
        return ""
    headers = {"X-API-Key": apikey, "Accept": "application/json", "Content-Type": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    config = {
        "method": "post", "cookie": False, "header": headers,
        "json": {"id": integer(torrent_id, minimum=1)},
        "allow_redirects": False, "proxy": proxy, "protocol": "HDArea",
        "result": "data.download_url", "result_base_url": site_url,
    }
    encoded = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrents/genDlToken"


class HDAreaSpider:
    """
    高清视界 API Key 索引器，搜索和空关键词最新列表共用零起始分页。
    """

    # 官方搜索接口单页最多返回 100 条。
    _size = 100
    # 搜索与其他专用 API 请求在适配器内逐次申请共享额度，编排层不重复计费。
    manages_request_throttle = True

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
        return download_enclosure(
            self._api_url, self._site_url, torrent_id, self._apikey, self._use_proxy, self._ua,
        )

    def _parse_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """
        将 API 种子字段投影为 MoviePilot 资源，直接使用服务端当前有效的促销倍率。

        :param item: data.items 中的一条种子记录
        :return: 标准种子字段；共享或未知分类保持未知，不靠 Torznab 聚合号猜测媒体类型
        :raises ValueError: 种子身份、标题、计数或倍率无效
        """
        torrent_id = integer(item.get("id"), minimum=1)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("HDArea API 种子标题无效")
        download_factor = (
            number(item["download_volume_factor"])
            if item.get("download_volume_factor") is not None else None
        )
        upload_factor = (
            number(item["upload_volume_factor"])
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
            "size": integer(item.get("size")),
            "seeders": integer(item.get("seeders", 0)),
            "peers": integer(item.get("leechers", 0)),
            "grabs": integer(item.get("times_completed", 0)),
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
            data = response_data(response.status_code, payload, self._apikey)
            items, pagination = data.get("items"), data.get("pagination")
            if not isinstance(items, list) or not isinstance(pagination, dict):
                raise ValueError("HDArea API 种子分页结构无效")
            if integer(pagination.get("page")) != page:
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
            self.error_detail = KEY_REQUIRED
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
            self.error_detail = KEY_REQUIRED
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

    def test_connection(self) -> tuple[bool, str]:
        """
        使用同一 REST API 认证和共享流控检测账号，不依赖 Cookie 或浏览器。

        :return: 账号是否可用及可以展示的认证、权限或接口失败原因
        """
        if not self._apikey:
            return False, KEY_REQUIRED
        response = None
        try:
            wait_for_site_request(self._rate_site)
            response = RequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
                url=f"{self._api_url}/user/profile", allow_redirects=False,
            )
            if response is None:
                raise ValueError("无法连接 HDArea API")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            data = response_data(response.status_code, payload, self._apikey)
            integer(data.get("id"), minimum=1)
            if data.get("enabled") is False:
                return False, "HDArea 账号已被禁用"
            return True, "连接成功"
        except (ValueError, RateLimitExceededException) as err:
            if torrent_rules.rate_limit_cooldown(str(err)) is not None:
                defer_site_request(self._rate_site, str(err))
            return False, str(err)
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def parse_download_response(status_code: Optional[int], payload: object, request_params: dict[str, Any]) -> str:
        """
        校验换票结果，兼容完整 download_url 或 dl_token，最终 GET 不携带 API Key。

        :param status_code: 原始 HTTP 状态，None 表示未取得响应
        :param payload: 响应 JSON，格式无效时为 None
        :param request_params: 当前网页来源、种子 ID 和认证配方
        :return: 本站同来源且匹配所选 ID 的临时下载地址
        :raises ValueError: 连接、换票、地址或资源身份校验失败
        """
        if status_code is None:
            raise ValueError("无法连接 HDArea API")
        apikey = str((request_params.get("header") or {}).get("X-API-Key") or "")
        data = response_data(status_code, payload, apikey)
        base_url = str(request_params.get("result_base_url") or "")
        torrent_id = str((request_params.get("json") or {}).get("id") or "")
        url = data.get("download_url")
        if not url and isinstance(data.get("dl_token"), str) and data["dl_token"]:
            download_page = urljoin(f"{base_url.rstrip('/')}/", "download.php")
            query = urlencode({"id": torrent_id, "dl_token": data["dl_token"]})
            url = f"{download_page}?{query}"
        if isinstance(url, str):
            url = urljoin(f"{base_url.rstrip('/')}/", url)
        url = UrlUtils.normalize_http_url(url)
        if url is None or len(url) > 2048 or not UrlUtils.same_origin(base_url, url):
            raise ValueError("HDArea API 未返回有效的本站下载地址")
        parts = urlparse(url)
        if parts.username is not None or parts.password is not None or parse_qs(parts.query).get("id") != [torrent_id]:
            raise ValueError("HDArea API 返回的下载地址与所选种子不符")
        logger.info("已获取到站点临时下载地址")
        return url
