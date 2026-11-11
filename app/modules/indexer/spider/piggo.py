"""
通过猪猪实验室 API 搜索和浏览种子，使用账号可见目录解释分类。
"""

import base64
import hashlib
import json
import re
from typing import Any, Optional, TypeAlias, cast
from urllib.parse import urljoin, urlparse

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.site.throttle import async_wait_for_site_request, defer_site_request, wait_for_site_request
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.foundation import temporal as time_tools
from app.foundation.url import UrlUtils
from app.runtime.cache import MemoryBackend
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 实验室公开文档指定的独立 API，不使用网页会话接口。
API_URL = "https://api.piggo.me/v1/api"
TOKEN_REQUIRED = "请在猪猪网页端「实验室」生成长期存取令牌，并填入站点 API Key 或 Token"

# 分类 ID 可跨分区复用；标签字典按实际分区保存，0 表示不限定分区的全站字典。
_Catalog: TypeAlias = tuple[dict[int, MediaType], dict[tuple[int, int], MediaType], dict[int, dict[int, str]]]


def access_token(apikey: Optional[str], token: Optional[str]) -> str:
    """
    选择长期存取令牌，优先使用非空 API Key，兼容 Token 配置。

    :param apikey: API Key 输入框中的实验室存取令牌
    :param token: 未填写 API Key 时使用的实验室存取令牌
    :return: 去掉首尾空白的令牌；两者都未配置时为空字符串
    """
    return str(apikey or "").strip() or str(token or "").strip()


def response_data(status_code: int, payload: object, token: str = "") -> dict[str, Any]:
    """
    校验 HTTP 和业务状态，阻止外层成功码掩盖必读公告。

    :param status_code: 原始 HTTP 响应状态码，非 200 不视为读取成功
    :param payload: 已解析的 JSON 响应
    :param token: 本次凭据，仅用于从站点错误文本中移除可能的回显
    :return: 成功响应的 data 对象
    :raises ValueError: 鉴权、必读公告、限流、其他业务失败或响应结构无效
    """
    # WAF 可能返回非 JSON 的鉴权或限流页，仍需保留 HTTP 失败语义。
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data")
    details = data if isinstance(data, dict) else {}
    code = str(payload.get("code"))
    inner_code = str(details.get("code", "0"))
    message = str(details.get("message") or payload.get("msg") or payload.get("message") or "").strip()
    if code == "notice_unread" or inner_code == "notice_unread":
        gate = details.get("notice_gate") or {}
        title = str(gate.get("title") or "") if isinstance(gate, dict) else ""
        message = f"有未读必读公告，请到猪猪网页端阅读后重试{f'：{title}' if title else ''}"
    elif status_code == 401 or code == "401" or inner_code == "401":
        message = "猪猪存取令牌已过期或无效，请重新生成长期存取令牌"
    elif status_code == 429 or code == "429" or inner_code == "429":
        message = f"站点限流：{message or 'HTTP 429，请稍后重试'}"
    elif not payload and status_code == 200:
        message = "猪猪 API 响应结构无效"
    elif status_code != 200 or code != "0" or inner_code != "0":
        failure_code = inner_code if inner_code != "0" else code
        message = f"猪猪 API 请求失败，HTTP {status_code}，业务码 {failure_code}{f'：{message}' if message else ''}"
    elif isinstance(data, dict):
        return data
    else:
        message = "猪猪 API 响应缺少有效 data"
    if token:
        message = message.replace(token, "[已隐藏]")
    raise ValueError(" ".join(message.split())[:300])


def integer(value: object, minimum: int = 0) -> int:
    """
    校验整数 ID、计数和字节字段，不截断浮点数或接受布尔值。

    :param value: API 返回的整数或十进制整数字符串
    :param minimum: 字段下限，ID 和页码使用 1，计数及字节使用 0
    :return: 已校验的整数
    :raises ValueError: 字段缺失、格式错误或小于下限；错误不回显原值
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise ValueError("猪猪 API 整数字段无效")
    number = int(value)
    if number < minimum:
        raise ValueError("猪猪 API 整数字段无效")
    return number


def download_enclosure(api_url: str, torrent_id: object, token: str, proxy: bool = False,
                       ua: Optional[str] = None) -> str:
    """
    构造取种子时才签发 URL 的两段式配方，不预先消耗下载次数。

    :param api_url: 实验室 API 基础地址
    :param torrent_id: 正整数站内种子 ID
    :param token: 当前长期存取令牌，缺失时不生成配方
    :param proxy: 换票是否使用系统代理
    :param ua: 换票请求的浏览器 User-Agent，避免被前置 WAF 当作默认 Python 客户端
    :return: Base64 请求配置和 download-link 地址；ID 或凭据缺失时为空字符串
    """
    if not token or not re.fullmatch(r"[1-9][0-9]*", str(torrent_id or "")):
        return ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if ua:
        headers["User-Agent"] = ua
    config = {
        "method": "post",
        "cookie": False,
        "header": headers,
        "json": {},
        "allow_redirects": False,
        "proxy": proxy,
        "protocol": "Piggo",
        "result": "data.url",
    }
    encoded = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrents/{torrent_id}/download-link"


class PiggoSpider:
    """
    猪猪长期存取令牌索引器；搜索结果不宣称 API 未提供的促销信息。
    """

    # 官方列表上限为每页 100 条；目录缓存最多保留 128 份，每份一小时。
    _size = 100
    _catalog_cache = MemoryBackend(maxsize=128, ttl=3600)
    _catalog_region = "piggo-catalog"
    # 一次搜索可能查询目录、列表和分区标签，每次实际 HTTP 请求自行申请共享额度。
    manages_request_throttle = True

    def __init__(self, indexer: dict[str, Any]) -> None:
        """
        保存认证和请求配置，目录缓存按 API 地址及凭据摘要隔离。

        :param indexer: 合并用户凭据的站点资源配置
        """
        self._name = indexer.get("name") or "猪猪"
        self._site_url = str(indexer.get("domain") or "https://piggo.me/")
        self._api_url = str(indexer.get("api_url") or API_URL).rstrip("/")
        self._token = access_token(indexer.get("apikey"), indexer.get("token"))
        self._proxy = get_runtime_setting("PROXY") if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._ua = indexer.get("ua") or get_runtime_setting("USER_AGENT")
        self._timeout = indexer.get("timeout") or 15
        self._rate_site = {key: indexer.get(key) for key in
                           ("id", "domain", "limit_seconds", "limit_interval", "limit_count")}
        self._rate_site["domain"] = site_rules.extract_domain(self._site_url)
        self._cache_key = hashlib.sha256(f"{self._api_url}\n{self._token}".encode("utf-8")).hexdigest()
        self.error_detail: Optional[str] = None

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        返回搜索与最新种子列表共用的分页容量。

        :param keyword: 保留统一入口签名，不改变猪猪分页容量
        :return: 每页最多 100 条
        """
        return cls._size

    def _headers(self) -> dict[str, str]:
        """
        为实验室请求构造 Bearer 和 JSON 头，不包含 Cookie 或前台签名。

        :return: 本次请求的独立头部字典
        """
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": self._ua}

    def _response_data(self, response: Any) -> dict[str, Any]:
        """
        读取有效业务数据，限流同步到搜索、用户数据和下载共用的站点额度。

        :param response: 同步或异步传输返回的响应，None 表示连接失败
        :return: 成功信封中的 data 对象
        :raises ValueError: API 或响应结构失败，同时删除当前凭据的目录缓存
        :raises RateLimitExceededException: 远端限流已经交给共享冷却
        """
        if response is None:
            raise ValueError("无法连接猪猪实验室 API")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        try:
            data = response_data(response.status_code, payload, self._token)
        except ValueError as err:
            self._catalog_cache.delete(self._cache_key, region=self._catalog_region)
            if torrent_rules.rate_limit_cooldown(str(err)) is not None:
                defer_site_request(self._rate_site, str(err))
                raise RateLimitExceededException(str(err)) from err
            raise
        return data

    def _request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        发起一次同步 POST JSON，不携带 Cookie、不跟随认证请求重定向。

        :param path: 实验室根地址下的相对接口路径
        :param body: JSON 参数对象；空对象仍发送 JSON 正文
        :return: 已校验的业务数据，响应在返回或失败前关闭
        """
        wait_for_site_request(self._rate_site)
        response = RequestUtils(headers=self._headers(), proxies=self._proxy, timeout=self._timeout).post_res(
            url=f"{self._api_url}/{path}", json=body, allow_redirects=False,
        )
        try:
            return self._response_data(response)
        finally:
            if response is not None:
                response.close()

    async def _async_request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        使用异步传输和共享流控发送 POST JSON，并在结束时释放响应。

        :param path: 实验室根地址下的相对接口路径
        :param body: JSON 参数对象；空对象仍发送 JSON 正文
        :return: 已校验的业务数据，取消或失败时沿用同一响应释放流程
        """
        await async_wait_for_site_request(self._rate_site)
        response = await AsyncRequestUtils(
            headers=self._headers(), proxies=self._proxy, timeout=self._timeout,
        ).post_res(url=f"{self._api_url}/{path}", json=body, allow_redirects=False)
        try:
            return self._response_data(response)
        finally:
            if response is not None:
                await response.aclose()

    @staticmethod
    def _parse_tags(value: object) -> dict[int, str]:
        """
        校验真实标签字典，只保留站点声明的 ID 与名称，不解释二级分类。

        :param value: catalog 的 tags 列表，旧接口未提供时使用空列表
        :return: 去除名称首尾空白的标签映射
        :raises ValueError: 列表、名称或正整数 ID 无效，或同字典中有重复 ID
        """
        if not isinstance(value, list):
            raise ValueError("猪猪标签字典无效")
        tags: dict[int, str] = {}
        try:
            for tag in value:
                tag_id = integer(tag["id"], minimum=1)
                name = tag["name"]
                if tag_id in tags or not isinstance(name, str) or not name.strip():
                    raise ValueError("猪猪标签数据无效")
                tags[tag_id] = name.strip()
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("猪猪标签字典无效") from err
        return tags

    @staticmethod
    def _tag_ids(item: dict[str, Any]) -> list[int]:
        """
        校验种子的独立标签 ID，重复 ID 保留首次顺序，缺失字段兼容旧接口。

        :param item: 实验室返回的种子条目
        :return: 独立标签 ID 列表，不读取 taxonomy_ids 或标题
        :raises ValueError: 标签列表或正整数 ID 无效
        """
        values = item.get("tag_ids", [])
        if not isinstance(values, list):
            raise ValueError("猪猪种子标签数据无效")
        return list(dict.fromkeys(integer(value, minimum=1) for value in values))

    @classmethod
    def _tag_labels(cls, item: dict[str, Any], catalogs: dict[int, dict[int, str]]) -> list[str]:
        """
        用实际分区字典解释标签，无法解析的 ID 不伪造名称或显示数值占位。
        """
        section_id = integer(item.get("section_id", 0))
        tags = catalogs.get(section_id, catalogs.get(0, {}))
        return list(dict.fromkeys(tags[tag_id] for tag_id in cls._tag_ids(item) if tag_id in tags))

    @staticmethod
    def _tag_sections(data: dict[str, Any], catalog: _Catalog) -> list[int]:
        """
        从已经验证的整页提取尚未缓存且确有标签的分区，避免逐种子请求目录。
        """
        return list(dict.fromkeys(
            integer(item["section_id"], minimum=1)
            for item in data["items"]
            if item.get("tag_ids") and item.get("section_id") and int(item["section_id"]) not in catalog[2]
        ))

    @classmethod
    def _apply_tag_labels(cls, torrents: list[dict[str, Any]], data: dict[str, Any], catalog: _Catalog) -> None:
        """
        分区目录全部读取成功后更新当前页标签，不改变资源顺序或分类事实。
        """
        for torrent, item in zip(torrents, data["items"], strict=True):
            torrent["labels"] = cls._tag_labels(item, catalog[2])

    @staticmethod
    def _name_media_type(name: str) -> MediaType:
        """
        根据当前目录名称识别媒体类型，动漫和纪录片等歧义名称不强行归类。

        :param name: 当前可见的分区或分类名称
        :return: 可明确识别的媒体类型，歧义或陌生名称返回未知
        """
        name = name.casefold()
        for media_type, labels in (
            (MediaType.MOVIE, ("电影", "電影", "movie", "cinema")),
            (MediaType.TV, ("剧", "劇", "综艺", "綜藝", "series", "television", "tv")),
            (MediaType.MUSIC, ("音乐", "音樂", "music", "演唱会", "演唱會", "concert")),
        ):
            if any(label in name for label in labels):
                return media_type
        return MediaType.UNKNOWN

    @classmethod
    def _parse_catalog(cls, data: dict[str, Any]) -> _Catalog:
        """
        校验并转换当前账号可见目录，不使用旧版固定分类 ID。

        :param data: 正式 catalog 响应的 sections 和 categories
        :return: 分区类型、分类成员关系及按分区隔离的标签字典；字典不包含账号或令牌
        :raises ValueError: 目录缺字段、整数无效、重复成员关系或分类引用未知分区
        """
        sections, categories = data.get("sections"), data.get("categories")
        if not isinstance(sections, list) or not isinstance(categories, list):
            raise ValueError("猪猪目录响应结构无效")
        section_map: dict[int, MediaType] = {}
        category_map: dict[tuple[int, int], MediaType] = {}
        try:
            for section in sections:
                section_id = integer(section["id"], minimum=1)
                if section_id in section_map or not isinstance(section["name"], str):
                    raise ValueError("猪猪分区数据无效")
                section_map[section_id] = cls._name_media_type(section["name"])
            for category in categories:
                category_id = integer(category["id"], minimum=1)
                section_id = integer(category["section_id"], minimum=1)
                membership = section_id, category_id
                if membership in category_map or section_id not in section_map or not isinstance(category["name"], str):
                    raise ValueError("猪猪分类数据无效")
                media_type = cls._name_media_type(category["name"])
                category_map[membership] = media_type if media_type != MediaType.UNKNOWN else section_map[section_id]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("猪猪目录响应结构无效") from err
        return section_map, category_map, {0: cls._parse_tags(data.get("tags", []))}

    def _load_catalog(self) -> _Catalog:
        """
        读取有界内存缓存；失败目录不缓存，也不回退到旧分类。

        :return: 当前 API 地址和凭据对应的可见目录
        """
        catalog = cast(Optional[_Catalog], self._catalog_cache.get(self._cache_key, region=self._catalog_region))
        if catalog is None:
            catalog = self._parse_catalog(self._request("catalog", {}))
            self._catalog_cache.set(self._cache_key, catalog, region=self._catalog_region)
        return catalog

    async def _async_load_catalog(self) -> _Catalog:
        """
        异步获取目录，缓存仅在短临界区访问内存，不阻塞执行网络操作。

        :return: 当前 API 地址和凭据对应的可见目录
        """
        catalog = cast(Optional[_Catalog], self._catalog_cache.get(self._cache_key, region=self._catalog_region))
        if catalog is None:
            catalog = self._parse_catalog(await self._async_request("catalog", {}))
            self._catalog_cache.set(self._cache_key, catalog, region=self._catalog_region)
        return catalog

    def _params(self, keyword: Optional[str], mtype: Optional[MediaType], cat: Optional[str],
                page: Optional[int], catalog: _Catalog) -> dict[str, Any]:
        """
        构造正式搜索字段，多个分区用分类并集保持全局分页，无法完整限定时不过滤。

        显式分类优先于媒体类型；不合并多路分区分页，也不把分类 ID 当作分区 ID。

        :param keyword: 标题关键词或完整 IMDb ID，空白时不添加搜索字段
        :param mtype: 可按目录解释的媒体类型，未知类型不缩窄查询
        :param cat: 显式分类 ID，可用逗号分隔
        :param page: MoviePilot 从零开始的页码
        :param catalog: 当前账号可见目录
        :return: 使用正式字段的 POST JSON 参数
        :raises ValueError: 显式分类 ID 格式无效
        """
        params: dict[str, Any] = {"page": int(page or 0) + 1, "per_page": self._size}
        value = str(keyword or "").strip()
        if value:
            if re.fullmatch(r"tt[0-9]+", value, re.IGNORECASE):
                params["imdb_id"] = value.lower()
            else:
                params["q"] = value
        if cat:
            if not re.fullmatch(r"[1-9][0-9]*(\s*,\s*[1-9][0-9]*)*", cat.strip()):
                raise ValueError("猪猪分类须填写当前目录中的正整数 ID，以逗号分隔")
            params["category_id"] = list(dict.fromkeys(int(value) for value in cat.split(",")))
            return params
        if mtype is None or mtype == MediaType.UNKNOWN:
            return params
        sections, categories, _tags = catalog
        # 纪录片、动漫、少儿等分区可同时含电影和剧集，未能判型时不排除这些可见资源。
        if MediaType.UNKNOWN in sections.values():
            return params
        matched = {section_id for section_id, media_type in sections.items() if media_type == mtype}
        if len(matched) == 1:
            params["section_id"] = next(iter(matched))
        elif matched and all(any(section_id == membership[0] for membership in categories) for section_id in matched):
            params["category_id"] = list(dict.fromkeys(category_id for section_id, category_id in categories
                                                     if section_id in matched))
        elif mtype and not matched:
            category_ids = list(dict.fromkeys(membership[1] for membership, media_type in categories.items()
                                              if media_type == mtype))
            if category_ids:
                params["category_id"] = category_ids
        return params

    @staticmethod
    def _torrent_media_type(item: dict[str, Any], media: dict[str, Any], catalog: _Catalog) -> MediaType:
        """
        优先使用作品类型，再解释当前目录；未知或相互冲突的分类不强行归类。

        :param item: 实验室返回的种子条目
        :param media: 条目中的作品标识对象
        :param catalog: 当前账号可见的分区和分类映射
        :return: 作品或目录可确认的媒体类型，否则为未知
        """
        media_type = {"movie": MediaType.MOVIE, "tv": MediaType.TV}.get(str(media.get("tmdb_kind") or ""))
        if media_type is not None:
            return media_type
        sections, categories, _tags = catalog
        category_ids = item.get("category_ids", [])
        if not isinstance(category_ids, list):
            raise ValueError("猪猪种子分类数据无效")
        section_id = integer(item.get("section_id", 0))
        known = set()
        for cid in category_ids:
            category = categories.get((section_id, integer(cid, minimum=1)))
            if category is not None and category != MediaType.UNKNOWN:
                known.add(category)
        if len(known) == 1:
            return next(iter(known))
        if known:
            return MediaType.UNKNOWN
        return sections.get(section_id, MediaType.UNKNOWN)

    @staticmethod
    def _media_identity(media: dict[str, Any]) -> tuple[Optional[MediaSource], Optional[str]]:
        """
        读取正式作品 ID，拒绝把对象、布尔值或任意文本作为精确识别身份。

        :param media: 实验室条目中的作品对象
        :return: 按 IMDb、豆瓣、TMDB 顺序选定的身份，全部缺失时返回空值
        :raises ValueError: 被选中的来源 ID 格式无效；错误不回显原值
        """
        for key, source in (("imdb", MediaSource.IMDb), ("douban", MediaSource.Douban), ("tmdb", MediaSource.TMDB)):
            value = media.get(key)
            if value in (None, ""):
                continue
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("猪猪作品标识数据无效")
            media_id = str(value).strip()
            pattern = r"tt[0-9]+" if source == MediaSource.IMDb else r"[1-9][0-9]*"
            if not re.fullmatch(pattern, media_id, re.IGNORECASE):
                raise ValueError("猪猪作品标识数据无效")
            return source, media_id.lower() if source == MediaSource.IMDb else media_id
        return None, None

    def _parse_item(self, item: dict[str, Any], catalog: _Catalog) -> dict[str, Any]:
        """
        转换正式种子字段，不编造实验室未提供的海报或促销倍率。

        :param item: 当前页的一条种子记录
        :param catalog: 当前账号可见目录
        :return: 可直接构造 TorrentInfo 的字段
        :raises ValueError: 必要字段、整数或媒体对象无效
        """
        torrent_id = integer(item["id"], minimum=1)
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError("猪猪种子名称无效")
        for field in ("small_descr", "page_url", "added_at"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ValueError("猪猪种子文本字段无效")
        media = item.get("media")
        if media is None:
            media = {}
        if not isinstance(media, dict):
            raise ValueError("猪猪作品标识数据无效")
        media_type = self._torrent_media_type(item, media, catalog)
        media_source, media_id = self._media_identity(media)
        return {
            "title": item["name"], "description": item.get("small_descr"),
            "enclosure": self.get_download_url(torrent_id),
            "page_url": item.get("page_url") or urljoin(self._site_url, f"detail/{torrent_id}"),
            "pubdate": time_tools.normalize_datetime(item.get("added_at")),
            "size": integer(item["size"]),
            "seeders": integer(item.get("seeders", 0)),
            "peers": integer(item.get("leechers", 0)),
            "grabs": integer(item.get("times_completed", 0)),
            "media_source": media_source, "media_id": media_id, "category": media_type.value,
            "labels": self._tag_labels(item, catalog[2]),
        }

    def _parse_result(self, data: dict[str, Any], catalog: _Catalog) -> list[dict[str, Any]]:
        """
        校验整页种子后发布结果，坏条目不能形成半页成功或提前结束自动分页。

        :param data: 已通过信封校验的列表响应
        :param catalog: 当前账号可见目录
        :return: 当前页的标准种子字段列表
        :raises ValueError: 列表结构、重复 ID 或条目字段无效
        """
        items = data.get("items")
        if not isinstance(items, list) or len(items) > self._size:
            raise ValueError("猪猪种子列表响应缺少有效 items")
        torrents: list[dict[str, Any]] = []
        ids: set[int] = set()
        try:
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("猪猪种子条目无效")
                torrent_id = integer(item["id"], minimum=1)
                if torrent_id in ids:
                    raise ValueError("猪猪种子 ID 重复")
                ids.add(torrent_id)
                torrents.append(self._parse_item(item, catalog))
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise ValueError("猪猪种子列表响应结构无效") from err
        return torrents

    def search(self, keyword: Optional[str], mtype: Optional[MediaType] = None, cat: Optional[str] = None,
               page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """
        同步搜索或浏览最新种子，不执行网页登录或提前签发下载链接。

        :param keyword: 标题关键词或完整 IMDb ID；为空时浏览最新种子
        :param mtype: 用当前目录限定的媒体类型
        :param cat: 显式分类 ID，可用逗号分隔
        :param page: 从零开始的 MoviePilot 页码
        :return: 是否失败及当前页标准种子列表
        :raises RateLimitExceededException: 限流交给索引模块分类和订阅调度，保留原始原因
        """
        self.error_detail = None
        try:
            if not self._token:
                raise ValueError(TOKEN_REQUIRED)
            catalog = self._load_catalog()
            data = self._request("torrents", self._params(keyword, mtype, cat, page, catalog))
            torrents = self._parse_result(data, catalog)
            for section_id in self._tag_sections(data, catalog):
                tags = self._request("catalog", {"section_id": section_id})
                catalog[2][section_id] = self._parse_tags(tags.get("tags", []))
            self._apply_tag_labels(torrents, data, catalog)
            return False, torrents
        except ValueError as err:
            self.error_detail = str(err)
            logger.warning(f"{self._name} 搜索失败：{self.error_detail}")
            return True, []

    async def async_search(self, keyword: Optional[str], mtype: Optional[MediaType] = None,
                           cat: Optional[str] = None, page: Optional[int] = 0) -> tuple[bool, list[dict[str, Any]]]:
        """
        异步执行同一实验室搜索协议，取消时不在后台继续请求。

        :param keyword: 标题关键词或完整 IMDb ID；为空时浏览最新种子
        :param mtype: 用当前目录限定的媒体类型
        :param cat: 显式分类 ID，可用逗号分隔
        :param page: 从零开始的 MoviePilot 页码
        :return: 是否失败及当前页标准种子列表
        :raises RateLimitExceededException: 限流交给索引模块分类和订阅调度，保留原始原因
        """
        self.error_detail = None
        try:
            if not self._token:
                raise ValueError(TOKEN_REQUIRED)
            catalog = await self._async_load_catalog()
            data = await self._async_request("torrents", self._params(keyword, mtype, cat, page, catalog))
            torrents = self._parse_result(data, catalog)
            for section_id in self._tag_sections(data, catalog):
                tags = await self._async_request("catalog", {"section_id": section_id})
                catalog[2][section_id] = self._parse_tags(tags.get("tags", []))
            self._apply_tag_labels(torrents, data, catalog)
            return False, torrents
        except ValueError as err:
            self.error_detail = str(err)
            logger.warning(f"{self._name} 搜索失败：{self.error_detail}")
            return True, []

    def get_download_url(self, torrent_id: object) -> str:
        """
        用当前凭据生成两段式下载配方，最终 GET 不携带 Bearer。

        :param torrent_id: 正整数站内种子 ID
        :return: 编码的 download-link 请求；ID 或令牌缺失时为空字符串
        """
        return download_enclosure(self._api_url, torrent_id, self._token, self._use_proxy, ua=self._ua)

    def test_connection(self) -> tuple[bool, str]:
        """
        使用同一实验室认证、流控和响应校验检查账号及下载权限，不访问网页。

        :return: 鉴权和下载权限是否有效，以及可展示的失败原因
        """
        if not self._token:
            return False, TOKEN_REQUIRED
        try:
            data = self._request("me", {})
            user = data.get("user")
            if not isinstance(user, dict):
                return False, "猪猪 API 未返回有效用户资料"
            integer(user.get("id"), minimum=1)
            if user.get("enabled") is False:
                return False, "猪猪账号已被禁用"
            if user.get("download_allowed") is False:
                return False, "当前猪猪账号没有下载权限"
            return True, "连接成功"
        except (ValueError, RateLimitExceededException) as err:
            return False, str(err)

    @staticmethod
    def parse_download_response(status_code: Optional[int], payload: object, request_params: dict[str, Any]) -> str:
        """
        解释实验室换票响应，不将网页下载主机替换为 API 主机。

        :param status_code: 原始 HTTP 状态，None 表示未取得响应
        :param payload: 响应 JSON，格式无效时为 None
        :param request_params: 当前认证配方，仅用 Bearer 从错误文本中移除凭据回显
        :return: 已校验的完整 HTTP 下载地址
        :raises ValueError: 连接、公告、鉴权、限流、响应或下载地址无效
        """
        if status_code is None:
            raise ValueError("无法连接猪猪实验室 API")
        headers = request_params.get("header") or {}
        token = str(headers.get("Authorization") or "").removeprefix("Bearer ")
        data = response_data(status_code, payload, token)
        url = UrlUtils.normalize_http_url(data.get("url"))
        if url is None or len(url) > 2048:
            raise ValueError("猪猪 API 未返回有效种子下载地址")
        parts = urlparse(url)
        if parts.username is not None or parts.password is not None:
            raise ValueError("猪猪 API 返回的下载地址含无效认证信息")
        logger.info("已获取到站点临时下载地址")
        return url
