"""
通过可乐（ColaPT）开放 API 搜索和浏览种子，不依赖 Cookie、网页会话或 M-Team 兼容层。
"""

import base64
import datetime
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.site.observation import report_site_search_page
from app.application.site.throttle import async_wait_for_site_request, defer_site_request, wait_for_site_request
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 搜索、用户数据需要「读取」范围，下载需要「下载」范围。
KEY_REQUIRED = "请在可乐「账号设置 → API Key」新建勾选「读取」和「下载」范围的 API Key，并填入站点 API Key"

# 站点注册域名，只用于解释本站详情页和签名下载路径。
_HOST = "colapt.com"
_DETAIL_PATH = re.compile(r"/torrents/([1-9][0-9]*)/?")
_DOWNLOAD_PATH = re.compile(r"/downloads/[A-Za-z0-9_-]+/([1-9][0-9]*)")
# 开放 API 的固定分类；动漫、纪录片和其他可能是电影也可能是剧集，结果保持未知。
_CATEGORIES = ("movie", "tv", "anime", "variety", "documentary", "other")
_CATEGORY_TYPES = {"movie": MediaType.MOVIE, "tv": MediaType.TV, "variety": MediaType.TV}
# 按媒体类型搜索时只排除明确属于另一类型的分类，共享分类的种子保留给后续识别。
_MEDIA_CATEGORIES = {MediaType.MOVIE: ("movie", "anime", "documentary", "other"),
                     MediaType.TV: ("tv", "anime", "variety", "documentary", "other")}


def response_data(status_code: int, payload: object, apikey: str = "") -> dict[str, Any]:
    """
    校验开放 API 响应，按错误码而不是 HTTP 状态猜测失败原因。

    :param status_code: 原始 HTTP 状态码，成功为 200 或 201
    :param payload: 已解码的 JSON；非 JSON 响应可以传入 None
    :param apikey: 本次密钥，仅用于移除错误消息中可能的凭据回显
    :return: 成功响应的 JSON 对象
    :raises ValueError: 鉴权、范围、权限、限流或响应结构无效
    """
    envelope = payload if isinstance(payload, dict) else {}
    raw_error = envelope.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    code = str(error.get("code") or "")
    message = str(error.get("message") or "").strip()
    if status_code in (200, 201) and isinstance(payload, dict) and "error" not in payload:
        return payload
    if status_code == 401 or code in ("authentication_required", "invalid_credentials"):
        message = "ColaPT API Key 已失效或无效"
    elif code == "api_key_scope_denied":
        message = "ColaPT API Key 缺少所需范围：搜索和用户数据需「读取」，下载需「下载」"
    elif code == "password_change_required":
        message = "ColaPT 账号需先在网页端修改密码"
    elif status_code == 429 or code == "rate_limited":
        retry_after = error.get("retry_after")
        wait = f"请 {retry_after} 秒后重试" if isinstance(retry_after, int) and retry_after > 0 else "请稍后重试"
        message = f"ColaPT 站点限流：{wait}"
    elif status_code == 403 or code == "permission_denied":
        message = f"ColaPT 账号无权进行此操作{f'：{message}' if message else ''}"
    elif status_code == 404 or code == "not_found":
        message = "ColaPT 种子不存在或无权查看"
    elif status_code in (200, 201):
        message = "ColaPT API 响应结构无效"
    else:
        message = f"ColaPT API 请求失败，HTTP {status_code}{f'：{message}' if message else ''}"
    if apikey:
        message = message.replace(apikey, "[已隐藏]")
    raise ValueError(" ".join(message.split())[:300])


def integer(value: object, minimum: int = 0) -> int:
    """
    校验 ID、计数和十进制字符串字节数，拒绝布尔值、浮点和负数。

    :param value: JSON 整数或十进制整数字符串
    :param minimum: 允许的最小值，种子 ID 使用 1
    :return: 已校验的整数
    :raises ValueError: 格式或范围无效；错误不回显原值
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise ValueError("ColaPT API 整数字段无效")
    result = int(value)
    if result < minimum:
        raise ValueError("ColaPT API 整数字段无效")
    return result


def decimal(value: object, signed: bool = False) -> float:
    """
    校验分享率和瓶盖余额这类定点小数字符串。

    :param value: 形如 1.2345 的十进制字符串
    :param signed: 是否允许负数，瓶盖余额可以为负
    :return: 有限数值
    :raises ValueError: 类型、格式或范围无效；错误不回显原值
    """
    pattern = r"-?(0|[1-9][0-9]*)(\.[0-9]+)?" if signed else r"(0|[1-9][0-9]*)(\.[0-9]+)?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("ColaPT API 数值字段无效")
    try:
        return float(Decimal(value))
    except (InvalidOperation, OverflowError) as err:
        raise ValueError("ColaPT API 数值字段无效") from err


def local_time(value: object) -> Optional[str]:
    """
    把 RFC 3339 时间转换为本地日期时间文本，与其他站点的发布时间口径一致。

    :param value: 带时区的 RFC 3339 时间或 None
    :return: 本地 %Y-%m-%d %H:%M:%S 文本；未提供时为 None
    :raises ValueError: 时间格式无效
    """
    if value is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError as err:
        raise ValueError("ColaPT API 时间字段无效") from err
    if parsed.tzinfo is None:
        raise ValueError("ColaPT API 时间字段无效")
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def download_enclosure(api_url: str, site_url: str, torrent_id: object, apikey: str,
                       proxy: bool = False, ua: Optional[str] = None) -> str:
    """
    构造下载时才签发 30 分钟签名地址的配方，搜索结果中不预先签发。

    :param api_url: 开放 API 基础地址
    :param site_url: 网页来源，用于校验签名地址的站点
    :param torrent_id: 正整数站内种子 ID
    :param apikey: 具备「下载」范围的 API Key
    :param proxy: 签发是否使用站点代理
    :param ua: 签发请求的 User-Agent
    :return: Base64 请求配置与 download-links 地址；缺少密钥时为空字符串
    :raises ValueError: 种子 ID 无效
    """
    if not apikey:
        return ""
    torrent_id = integer(torrent_id, minimum=1)
    headers = {"X-API-Key": apikey, "Accept": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    config = {
        "method": "post", "cookie": False, "header": headers, "allow_redirects": False, "proxy": proxy,
        "protocol": "ColaPT", "result": "download_url", "result_base_url": site_url, "torrent_id": torrent_id,
    }
    encoded = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrents/{torrent_id}/download-links"


class ColaPTSpider:
    """
    可乐开放 API 索引器，搜索与空关键词最新列表共用按种子分页的 view=torrents。
    """

    # listMedia 单页最多 100 条。
    _size = 100
    # 搜索与其他开放 API 请求在适配器内逐次申请共享额度，编排层不重复计费。
    manages_request_throttle = True

    def __init__(self, indexer: dict[str, Any]) -> None:
        """
        保存站点认证与流控配置，网页 Cookie 不参与 API 请求。

        :param indexer: 合并当前用户配置的站点资源
        """
        self._name = indexer.get("name") or "可乐"
        self._site_url = str(indexer.get("domain") or "https://www.colapt.com/").rstrip("/")
        self._api_url = str(indexer.get("api_url") or f"{self._site_url}/api/v1").rstrip("/")
        self._apikey = str(indexer.get("apikey") or "").strip()
        self._ua = indexer.get("ua") or get_runtime_setting("USER_AGENT")
        self._proxy = get_runtime_setting("PROXY") if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._timeout = indexer.get("timeout") or 15
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

    @staticmethod
    def torrent_id_from_url(url: str) -> Optional[str]:
        """
        从本站详情页或签名下载路径提取稳定种子 ID，签名凭据不参与身份。

        :param url: 搜索候选、旧缓存或下载重定向中的公开地址
        :return: 种子 ID；其他站点或其他路径返回 None
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host != _HOST and not host.endswith(f".{_HOST}"):
            return None
        match = _DETAIL_PATH.fullmatch(parsed.path) or _DOWNLOAD_PATH.fullmatch(parsed.path)
        return match[1] if match else None

    @staticmethod
    def is_expiring_download_url(url: str) -> bool:
        """
        只把本站带 exp 和 sig 的 /downloads/{凭据}/{id} 地址解释为短时签名。

        :param url: 原始或重定向后的下载地址
        :return: 是否为本站签名下载地址
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        query = parse_qs(parsed.query)
        return ((host == _HOST or host.endswith(f".{_HOST}")) and _DOWNLOAD_PATH.fullmatch(parsed.path) is not None
                and bool(query.get("exp")) and bool(query.get("sig")))

    def _headers(self) -> dict[str, str]:
        """
        构造仅使用 API Key 的 JSON 请求头，不带 Cookie、Authorization 或 Origin。

        :return: 独立请求头字典
        """
        return {"X-API-Key": self._apikey, "Accept": "application/json", "User-Agent": self._ua}

    @staticmethod
    def _categories(mtype: Optional[MediaType], cat: Optional[str]) -> Optional[str]:
        """
        把站点分类或媒体类型转换为服务端多选分类，一次请求完成筛选。

        :param mtype: 未指定分类时使用的媒体类型
        :param cat: 逗号分隔的开放 API 分类值，显式分类优先，未知值忽略
        :return: 去重后逗号分隔的 category 参数；不筛选时为 None
        """
        parts = (part.strip() for part in str(cat or "").split(","))
        selected = list(dict.fromkeys(value for value in parts if value in _CATEGORIES))
        if not selected and mtype:
            selected = list(_MEDIA_CATEGORIES.get(mtype, ()))
        return ",".join(selected) or None

    def _params(self, keyword: Optional[str], category: Optional[str], page: Optional[int]) -> dict[str, Any]:
        """
        构造按种子全局分页的查询，完整 IMDb ID 使用精确筛选，最新列表按发布时间倒序且不置顶官方。

        :param keyword: 普通关键词、完整 IMDb ID 或空值
        :param category: 逗号分隔的服务端分类，无需筛选时为 None
        :param page: 从 0 开始的页码
        :return: 开放 API 查询参数，不包含密钥
        """
        params: dict[str, Any] = {"view": "torrents", "sort": "latest", "direction": "desc",
                                  "official_first": "false", "page": int(page or 0) + 1, "page_size": self._size}
        keyword = str(keyword or "").strip()
        if keyword:
            if re.fullmatch(r"tt[0-9]{7,10}", keyword):
                params["imdb_id"] = keyword
            else:
                params["q"] = keyword[:255]
        if category:
            params["category"] = category
        return params

    def get_download_url(self, torrent_id: object) -> str:
        """
        使用当前密钥按站内 ID 重建签发配方，不重新搜索或预先签发地址。

        :param torrent_id: 正整数站内种子 ID
        :return: 下载时才调用 createDownloadLink 的配方
        """
        return download_enclosure(self._api_url, self._site_url, torrent_id, self._apikey, self._use_proxy, self._ua)

    @staticmethod
    def _promotion(item: dict[str, Any]) -> tuple[float, float, Optional[str]]:
        """
        读取服务端当前计费的促销倍率；没有促销即正常计费。

        :param item: 一条种子版本
        :return: 下载倍率、上传倍率和促销结束时间
        :raises ValueError: 倍率结构无效
        """
        promotion = item.get("promotion")
        if promotion is None:
            return 1.0, 1.0, None
        if not isinstance(promotion, dict):
            raise ValueError("ColaPT API 促销字段无效")
        factors = []
        for name in ("download", "upload"):
            ratio = promotion.get(name)
            if not isinstance(ratio, dict):
                raise ValueError("ColaPT API 促销字段无效")
            factors.append(integer(ratio.get("num")) / integer(ratio.get("den"), minimum=1))
        active = factors != [1.0, 1.0]
        return factors[0], factors[1], local_time(promotion.get("ends_at")) if active else None

    def _parse_group(self, group: object) -> Optional[dict[str, Any]]:
        """
        将 view=torrents 的单版本分组投影为 MoviePilot 资源。

        :param group: items 中的一条分组，每组至多一个版本
        :return: 标准种子字段；没有版本的作品返回 None
        :raises ValueError: 分组结构、种子身份、标题、计数、倍率或 H&R 标记无效
        """
        if not isinstance(group, dict) or not isinstance(group.get("versions"), list):
            raise ValueError("ColaPT API 种子条目无效")
        if not group["versions"]:
            return None
        item, media = group["versions"][0], group.get("media")
        if not isinstance(item, dict) or not isinstance(media, (dict, type(None))):
            raise ValueError("ColaPT API 种子条目无效")
        media = media or {}
        torrent_id = integer(item.get("id"), minimum=1)
        if not isinstance(item.get("release_name"), str) or not item["release_name"].strip():
            raise ValueError("ColaPT API 种子标题无效")
        download_factor, upload_factor, freedate = self._promotion(item)
        if not isinstance(item.get("hit_and_run", False), bool):
            raise ValueError("ColaPT API H&R 字段无效")
        result = {
            "title": item["release_name"], "description": item.get("subtitle") or media.get("title"),
            "enclosure": self.get_download_url(torrent_id), "page_url": f"{self._site_url}/torrents/{torrent_id}",
            "pubdate": local_time(item.get("published_at")), "size": integer(item.get("size_bytes")),
            "seeders": integer(item.get("seeders") or 0), "peers": integer(item.get("leechers") or 0),
            "grabs": integer(item.get("completions") or 0),
            "downloadvolumefactor": download_factor, "uploadvolumefactor": upload_factor, "freedate": freedate,
            "category": _CATEGORY_TYPES.get(str(media.get("category") or ""), MediaType.UNKNOWN).value,
            "hit_and_run": item.get("hit_and_run", False),
            "labels": [tag for tag in item.get("tags") or [] if isinstance(tag, str)],
        }
        if re.fullmatch(r"tt[0-9]{7,10}", str(media.get("imdb_id") or "")):
            result.update(media_source=MediaSource.IMDb, media_id=media["imdb_id"])
        elif re.fullmatch(r"[1-9][0-9]*", str(media.get("douban_id") or "")):
            result.update(media_source=MediaSource.Douban, media_id=media["douban_id"])
        return result

    def _process_response(self, response: Any, page: int) -> tuple[bool, list[dict[str, Any]]]:
        """
        校验分页事实与条目，错误不能被当成成功空列表，限流传播到共享站点冷却。

        :param response: 同步或异步传输响应，None 表示连接失败
        :param page: 本次请求的从 1 开始的页码
        :return: 是否失败及当前页资源
        :raises RateLimitExceededException: 远端已明确要求冷却
        """
        try:
            if response is None:
                raise ValueError("无法连接 ColaPT API")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            data = response_data(response.status_code, payload, self._apikey)
            items = data.get("items")
            if not isinstance(items, list):
                raise ValueError("ColaPT API 种子分页结构无效")
            if integer(data.get("page"), minimum=1) != page:
                raise ValueError("ColaPT API 返回了错误页码")
            total_pages = integer(data.get("total_pages"))
            results = []
            for group in items:
                parsed = self._parse_group(group)
                if parsed is not None:
                    results.append(parsed)
            report_site_search_page(raw_count=len(items), has_more=page < total_pages)
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
        同步搜索或浏览最新种子，不回退到 Cookie 页面或 M-Team 兼容层。

        :param keyword: 普通关键词、完整 IMDb ID 或空值
        :param mtype: 未指定分类时使用的媒体类型
        :param cat: 逗号分隔的开放 API 分类值
        :param page: 零起始页码
        :return: 是否失败及当前页资源
        """
        if not self._apikey:
            self.error_detail = KEY_REQUIRED
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        category = self._categories(mtype, cat)
        params = self._params(keyword, category, page)
        wait_for_site_request(self._rate_site)
        response = RequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
            url=f"{self._api_url}/media", params=params, allow_redirects=False,
        )
        try:
            return self._process_response(response, params["page"])
        finally:
            if response is not None:
                response.close()

    async def async_search(self, keyword: Optional[str], mtype: Optional[MediaType] = None, cat: Optional[str] = None,
                           page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """
        异步执行相同的查询、分页和筛选合同。

        :param keyword: 普通关键词、完整 IMDb ID 或空值
        :param mtype: 未指定分类时使用的媒体类型
        :param cat: 逗号分隔的开放 API 分类值
        :param page: 零起始页码
        :return: 是否失败及当前页资源
        """
        if not self._apikey:
            self.error_detail = KEY_REQUIRED
            logger.warning(f"{self._name} {self.error_detail}")
            return True, []
        category = self._categories(mtype, cat)
        params = self._params(keyword, category, page)
        await async_wait_for_site_request(self._rate_site)
        response = await AsyncRequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
            url=f"{self._api_url}/media", params=params, allow_redirects=False,
        )
        try:
            return self._process_response(response, params["page"])
        finally:
            if response is not None:
                await response.aclose()

    def test_connection(self) -> tuple[bool, str]:
        """
        使用会员数据接口验证 API Key 的「读取」范围和账号身份，不依赖 Cookie 或浏览器。

        :return: 账号是否可用及可以展示的认证、范围或接口失败原因
        """
        if not self._apikey:
            return False, KEY_REQUIRED
        response = None
        try:
            wait_for_site_request(self._rate_site)
            response = RequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).get_res(
                url=f"{self._api_url}/me/stats", allow_redirects=False,
            )
            if response is None:
                raise ValueError("无法连接 ColaPT API")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            data = response_data(response.status_code, payload, self._apikey)
            user = data.get("user")
            integer(user.get("id") if isinstance(user, dict) else None, minimum=1)
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
        校验签发结果，只接受本站、所选种子且带签名的 HTTPS 地址；最终 GET 不携带 API Key。

        :param status_code: 原始 HTTP 状态，None 表示未取得响应
        :param payload: 响应 JSON，格式无效时为 None
        :param request_params: 当前网页来源、种子 ID 和认证配方
        :return: 30 分钟内有效的签名下载地址
        :raises ValueError: 连接、签发、地址或资源身份校验失败
        """
        if status_code is None:
            raise ValueError("无法连接 ColaPT API")
        apikey = str((request_params.get("header") or {}).get("X-API-Key") or "")
        data = response_data(status_code, payload, apikey)
        url = UrlUtils.normalize_http_url(data.get("download_url"))
        base_url = str(request_params.get("result_base_url") or "")
        if url is None or len(url) > 2048 or not url.startswith("https://"):
            raise ValueError("ColaPT API 未返回有效的本站下载地址")
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None \
                or site_rules.extract_domain(url) != site_rules.extract_domain(base_url) \
                or not ColaPTSpider.is_expiring_download_url(url):
            raise ValueError("ColaPT API 未返回有效的本站下载地址")
        if ColaPTSpider.torrent_id_from_url(url) != str(request_params.get("torrent_id") or ""):
            raise ValueError("ColaPT API 返回的下载地址与所选种子不符")
        logger.info("已获取到站点临时下载地址")
        return url
