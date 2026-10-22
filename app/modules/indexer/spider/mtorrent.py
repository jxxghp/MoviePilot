"""
通过 M-Team 接口搜索种子并转换为通用索引结果。
"""

import base64
import json
import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.configuration import get_configured_system_config
from app.application.site.observation import report_site_search_page
from app.domain import site as site_rules
from app.foundation import temporal as time_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


class MTorrentSpider:
    """
    mTorrent API 搜索与字幕下载适配器。
    """
    _indexerid = None
    _domain = None
    _url = None
    _name: Optional[str] = ""
    _proxy = None
    _cookie = None
    _ua = None
    _size = 100
    _subtitle_size = 20
    _api_domain: Optional[str] = None
    _searchurl = "https://%s/api/torrent/search"
    _downloadurl = "https://%s/api/torrent/genDlToken"
    _subtitle_list_url = "https://%s/api/subtitle/list"
    _subtitle_genlink_url = "https://%s/api/subtitle/genlink"
    _subtitle_download_url ="https://%s/api/subtitle/dlV2?credential=%s"
    _pageurl = "%sdetail/%s"
    _timeout = 15

    # 电影分类
    _movie_category = ['401', '419', '420', '421', '439', '405', '404']
    # 电视剧分类
    _tv_category = ['403', '402', '435', '438', '404', '405']
    # 音乐分类：434 为无损音乐，406 为演唱分区
    _music_category = ['434', '406']

    # 站点 API 访问密钥。
    _apikey = None
    # 站点 JWT 身份令牌。
    _token = None
    # 最近一次字幕搜索失败原因
    error_detail: Optional[str] = None

    # 标签
    _labels = {
        "0": "",
        "1": "DIY",
        "2": "国配",
        "3": "DIY 国配",
        "4": "中字",
        "5": "DIY 中字",
        "6": "国配 中字",
        "7": "DIY 国配 中字"
    }

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量。

        :param keyword: 保留统一入口参数，不影响本站的分页容量
        :return: 本站搜索接口的固定单页记录数
        """
        return cls._size

    def __init__(self, indexer: dict[str, Any]):
        """
        使用站点配置初始化 M-Team API 请求上下文。
        """
        self.systemconfig = get_configured_system_config()
        if indexer:
            self._indexerid = indexer.get('id')
            self._url = indexer.get('domain')
            self._domain = site_rules.extract_domain(str(self._url or ""))
            self._api_domain = self.__resolve_api_domain(indexer)
            self._searchurl = self._searchurl % self._api_domain
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = get_runtime_setting('PROXY')
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._apikey = indexer.get('apikey')
            self._token = indexer.get('token')
            self._timeout = indexer.get('timeout') or 15
            self.error_detail = None

    @staticmethod
    def __resolve_api_domain(indexer: dict[str, Any]) -> str:
        """
        从站点资源配置解析 M-Team API 主机。

        资源可以直接提供 ``api_domain`` 或 ``api_host``；未提供时沿用
        M-Team 的 ``api.<注册域名>`` 约定，避免把旧网页接口写死在代码中。
        """
        configured = indexer.get("api_domain") or indexer.get("api_host")
        if configured:
            value = str(configured).strip()
            parsed = urlparse(value if "://" in value else f"//{value}")
            host = parsed.netloc or parsed.path.split("/", 1)[0]
            if host:
                return host

        domain_url = str(indexer.get("domain") or "").strip()
        parsed = urlparse(domain_url if "://" in domain_url else f"//{domain_url}")
        host = parsed.netloc or parsed.path.split("/", 1)[0]
        if host.startswith("api."):
            return host
        registered_domain = site_rules.extract_domain(domain_url)
        return f"api.{registered_domain}" if registered_domain else host

    def __get_params(self, keyword: str, mtype: Optional[MediaType] = None, page: Optional[int] = 0,
                     cat: Optional[str] = None) -> dict[str, object]:
        """
        构造请求参数，显式站点分类优先于媒体类型预置分类。

        :param keyword: 搜索关键词，IMDb 标识会转换为完整链接
        :param mtype: 未指定站点分类时使用的媒体类型
        :param page: 从 0 开始的页码，提交到站点时转换为从 1 开始
        :param cat: 逗号分隔的站点分类 ID，未指定时沿用媒体类型筛选
        :return: M-Team 搜索请求体
        """
        if cat:
            categories = [category.strip() for category in cat.split(",")]
        elif not mtype:
            categories = []
        elif mtype == MediaType.TV:
            categories = self._tv_category
        elif mtype == MediaType.MUSIC:
            categories = self._music_category
        else:
            categories = self._movie_category
        # mtorrent搜索imdb需要输入完整imdb链接，参见 https://wiki.m-team.cc/zh-tw/imdbtosearch
        if keyword and keyword.startswith("tt"):
            keyword = f"https://www.imdb.com/title/{keyword}"
        return {
            "keyword": keyword,
            "categories": categories,
            "pageNumber": int(page or 0) + 1,
            "pageSize": self._size,
            "visible": 1
        }

    def __parse_result(self, results: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """
        解析搜索结果。
        """
        torrents: list[dict[str, object]] = []
        if not results:
            return torrents

        for result in results:
            category_value = result.get('category')
            if category_value in self._music_category:
                category = MediaType.MUSIC.value
            elif category_value in self._tv_category \
                    and category_value not in self._movie_category:
                category = MediaType.TV.value
            elif category_value in self._movie_category:
                category = MediaType.MOVIE.value
            else:
                category = MediaType.UNKNOWN.value
            # 处理馒头新版标签
            labels = []
            labels_new = result.get('labelsNew')
            if labels_new:
                # 新版标签本身就是list
                labels = labels_new
            else:
                # 旧版标签
                labels_value = self._labels.get(result.get('labels') or "0") or ""
                if labels_value:
                    labels = labels_value.split()
            status = result.get('status', {})
            torrent = {
                'title': result.get('name'),
                'description': result.get('smallDescr'),
                'enclosure': self.__get_download_url(result.get('id')),
                'pubdate': time_tools.format_timestamp(result.get('createdDate')),
                'size': int(result.get('size') or '0'),
                'seeders': int(status.get("seeders") or '0'),
                'peers': int(status.get("leechers") or '0'),
                'grabs': int(status.get("timesCompleted") or '0'),
                'downloadvolumefactor': self.__get_downloadvolumefactor(status.get("discount")),
                'uploadvolumefactor': self.__get_uploadvolumefactor(status.get("discount")),
                'page_url': self._pageurl % (self._url, result.get('id')),
                'imdbid': self.__find_imdbid(result.get('imdb')),
                'labels': labels,
                'category': category
            }
            if discount_end_time := status.get('discountEndTime'):
                torrent['freedate'] = time_tools.format_timestamp(discount_end_time)
            # 解析全站促销时的规则(当前馒头只有下载促销)
            if promotion_rule := status.get("promotionRule"):
                discount = promotion_rule.get("discount", "NORMAL")
                torrent["downloadvolumefactor"] = self.__get_downloadvolumefactor(discount)
                if end_time := promotion_rule.get("endTime"):
                    torrent["freedate"] = time_tools.format_timestamp(end_time)
            if mall_single_free := status.get("mallSingleFree"):
                if mall_single_free.get("status") == "ONGOING":
                    torrent["downloadvolumefactor"] = self.__get_downloadvolumefactor("FREE")
                    if end_date := mall_single_free.get("endDate"):
                        torrent["freedate"] = time_tools.format_timestamp(end_date)
            torrents.append(torrent)
        return torrents

    def __process_response(self, res: Any) -> Tuple[bool, List[dict[str, Any]]]:
        """
        统一判定搜索响应状态并投影 M-Team 种子结果。
        """
        if res and res.status_code == 200:
            payload = res.json()
            if str(payload.get("code")) != "0" or not isinstance(payload.get("data"), dict):
                logger.warning(f"{self._name} 搜索失败，站点返回业务错误")
                return True, []
            data = payload["data"]
            results = data.get("data") or []
            # 分页事实只供逐页搜索判断末页，空结果仍按末页处理。
            page_number, total_pages = data.get("pageNumber"), data.get("totalPages")
            has_more = page_number < total_pages if isinstance(page_number, int) and isinstance(total_pages, int) else None
            report_site_search_page(raw_count=len(results), has_more=has_more)
            return False, self.__parse_result(results)
        if res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
        return True, []

    def search(self, keyword: str, mtype: Optional[MediaType] = None, page: Optional[int] = 0,
               search_type: Optional[str] = None, cat: Optional[str] = None) -> Tuple[bool, List[dict[str, Any]]]:
        """
        搜索种子或字幕。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :param page: 从 0 开始的搜索页码
        :param search_type: 资源类型；subtitles 表示字幕，其余值搜索种子
        :param cat: 逗号分隔的站点分类 ID，优先于媒体类型，仅对种子搜索生效
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if search_type == "subtitles":
            return self.search_subtitles(keyword=keyword, page=page)
        if not self._apikey:
            return True, []

        params = self.__get_params(keyword, mtype, page, cat)

        res = RequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey
            },
            proxies=self._proxy,
            referer=f"{self._domain}browse",
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        return self.__process_response(res)

    async def async_search(self, keyword: str, mtype: Optional[MediaType] = None, page: Optional[int] = 0,
                           search_type: Optional[str] = None, cat: Optional[str] = None) -> Tuple[bool, List[dict[str, Any]]]:
        """
        异步搜索种子或字幕。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :param page: 从 0 开始的搜索页码
        :param search_type: 资源类型；subtitles 表示字幕，其余值搜索种子
        :param cat: 逗号分隔的站点分类 ID，优先于媒体类型，仅对种子搜索生效
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if search_type == "subtitles":
            return await self.async_search_subtitles(keyword=keyword, page=page)
        if not self._apikey:
            return True, []

        params = self.__get_params(keyword, mtype, page, cat)

        res = await AsyncRequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey
            },
            proxies=self._proxy,
            referer=f"{self._domain}browse",
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        return self.__process_response(res)

    def __get_subtitle_params(self, keyword: str, page: Optional[int] = 0) -> dict[str, Any]:
        """
        构造 M-Team 字幕搜索请求体。
        """
        return {
            "keyword": keyword,
            "pageNumber": int(page or 0) + 1,
            "pageSize": self._subtitle_size,
        }

    @staticmethod
    def __get_subtitle_rows(payload: Any) -> List[dict[str, Any]]:
        """
        兼容 M-Team 字幕接口分页数据的不同包装层级。
        """
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("data", "items", "records", "list"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, dict)]
        return []

    @staticmethod
    def __get_int(value: Any, default: int = 0) -> int:
        """
        把接口中的数字字段安全转换为整数。
        """
        try:
            return default if value is None else int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __parse_size(value: Any) -> int:
        """
        把字幕接口返回的字节数或带单位容量转换为字节数。
        """
        if value is None or value == "":
            return 0
        normalized = str(value).replace(",", "").replace(" ", "").upper()
        if normalized.isdigit():
            return int(normalized)
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPI]?I?B?)", normalized)
        if not match:
            return 0
        size = float(match.group(1))
        unit = match.group(2)
        units = {
            "KB": 1024,
            "KIB": 1024,
            "MB": 1024 ** 2,
            "MIB": 1024 ** 2,
            "GB": 1024 ** 3,
            "GIB": 1024 ** 3,
            "TB": 1024 ** 4,
            "TIB": 1024 ** 4,
            "PB": 1024 ** 5,
            "PIB": 1024 ** 5,
        }
        return round(size * units.get(unit, 1))

    def __parse_subtitle_rows(self, results: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """
        解析 M-Team 字幕结果并为每条记录生成可直接下载的签名地址。
        """
        subtitles = []
        for result in results:
            subtitle_id = result.get("id")
            if subtitle_id is None:
                continue
            torrent = result.get("torrent")
            torrent_id = torrent.get("id") if isinstance(torrent, dict) else torrent
            link = self.__subtitle_genlink(str(subtitle_id))
            if not link:
                continue
            title = result.get("name") or result.get("filename") or str(subtitle_id)
            file_name = result.get("filename") or result.get("fileName")
            created = result.get("createdDate") or result.get("lastModifiedDate")
            pubdate: Optional[str] = None
            if isinstance(created, (int, float)) or str(created or "").isdigit():
                pubdate = time_tools.format_timestamp(str(created))
            else:
                pubdate = time_tools.normalize_datetime(str(created)) if created else None
            subtitles.append({
                "title": title,
                "description": title,
                "enclosure": link,
                "page_url": self._pageurl % (self._url, torrent_id) if torrent_id else None,
                "language": result.get("lang") or result.get("language") or "",
                "size": self.__parse_size(result.get("size")),
                "pubdate": pubdate,
                "grabs": self.__get_int(result.get("hits")),
                "uploader": result.get("author"),
                "torrent_id": str(torrent_id) if torrent_id is not None else None,
                "subtitle_id": str(subtitle_id),
                "file_name": file_name,
            })
        return subtitles

    def __process_subtitle_response(self, res: Any) -> Tuple[bool, List[dict[str, Any]]]:
        """
        判定字幕搜索响应并转换为 MoviePilot 字幕字段。
        """
        self.error_detail = None
        if res is None:
            self.error_detail = f"无法连接 {self._api_domain or self._domain}"
            logger.warn(f"{self._name} 字幕搜索失败，{self.error_detail}")
            return True, []
        if res.status_code != 200:
            self.error_detail = f"HTTP {res.status_code}"
            logger.warn(f"{self._name} 字幕搜索失败，错误码：{res.status_code}")
            return True, []
        try:
            payload = res.json()
        except (TypeError, ValueError):
            self.error_detail = "响应不是有效 JSON"
            logger.warn(f"{self._name} 字幕搜索失败，{self.error_detail}")
            return True, []
        if not isinstance(payload, dict) or self.__get_int(payload.get("code"), -1) != 0:
            self.error_detail = str(payload.get("message") or "接口返回失败") if isinstance(payload, dict) else "接口返回失败"
            logger.warn(f"{self._name} 字幕搜索失败，返回：{self.error_detail}")
            return True, []
        return False, self.__parse_subtitle_rows(self.__get_subtitle_rows(payload))

    def search_subtitles(self, keyword: str, page: Optional[int] = 0) -> Tuple[bool, List[dict[str, Any]]]:
        """
        通过 M-Team 字幕 API 搜索字幕。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param page: 从 0 开始的搜索页码
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if not self._apikey:
            self.error_detail = "未配置 API Key"
            return True, []
        params = self.__get_subtitle_params(keyword, page)
        res = RequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey,
            },
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(url=f"https://{self._api_domain}/api/subtitle/search", json=params)
        return self.__process_subtitle_response(res)

    async def __async_subtitle_genlink(self, subtitle_id: str) -> Optional[str]:
        """
        异步获取字幕下载链接。
        """
        url = self._subtitle_genlink_url % self._api_domain
        res = await AsyncRequestUtils(
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey,
            },
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(url, data={"id": subtitle_id})
        if res and res.status_code == 200:
            result = res.json()
            if self.__get_int(result.get("code"), -1) == 0 and isinstance(result.get("data"), str):
                return self._subtitle_download_url % (self._api_domain, result["data"])
            return None
        return None

    async def __async_parse_subtitle_rows(self, results: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """
        异步解析 M-Team 字幕结果并生成下载地址。
        """
        subtitles = []
        for result in results:
            subtitle_id = result.get("id")
            if subtitle_id is None:
                continue
            torrent = result.get("torrent")
            torrent_id = torrent.get("id") if isinstance(torrent, dict) else torrent
            link = await self.__async_subtitle_genlink(str(subtitle_id))
            if not link:
                continue
            title = result.get("name") or result.get("filename") or str(subtitle_id)
            file_name = result.get("filename") or result.get("fileName")
            created = result.get("createdDate") or result.get("lastModifiedDate")
            pubdate: Optional[str] = None
            if isinstance(created, (int, float)) or str(created or "").isdigit():
                pubdate = time_tools.format_timestamp(str(created))
            else:
                pubdate = time_tools.normalize_datetime(str(created)) if created else None
            subtitles.append({
                "title": title,
                "description": title,
                "enclosure": link,
                "page_url": self._pageurl % (self._url, torrent_id) if torrent_id else None,
                "language": result.get("lang") or result.get("language") or "",
                "size": self.__parse_size(result.get("size")),
                "pubdate": pubdate,
                "grabs": self.__get_int(result.get("hits")),
                "uploader": result.get("author"),
                "torrent_id": str(torrent_id) if torrent_id is not None else None,
                "subtitle_id": str(subtitle_id),
                "file_name": file_name,
            })
        return subtitles

    async def async_search_subtitles(self, keyword: str, page: Optional[int] = 0) -> Tuple[bool, List[dict[str, Any]]]:
        """
        异步通过 M-Team 字幕 API 搜索字幕。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param page: 从 0 开始的搜索页码
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if not self._apikey:
            self.error_detail = "未配置 API Key"
            return True, []
        params = self.__get_subtitle_params(keyword, page)
        res = await AsyncRequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey,
            },
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(url=f"https://{self._api_domain}/api/subtitle/search", json=params)
        self.error_detail = None
        if res is None:
            self.error_detail = f"无法连接 {self._api_domain or self._domain}"
            return True, []
        if res.status_code != 200:
            self.error_detail = f"HTTP {res.status_code}"
            return True, []
        try:
            payload = res.json()
        except (TypeError, ValueError):
            self.error_detail = "响应不是有效 JSON"
            return True, []
        if not isinstance(payload, dict) or self.__get_int(payload.get("code"), -1) != 0:
            self.error_detail = str(payload.get("message") or "接口返回失败") if isinstance(payload, dict) else "接口返回失败"
            return True, []
        return False, await self.__async_parse_subtitle_rows(self.__get_subtitle_rows(payload))

    @staticmethod
    def __find_imdbid(imdb: Optional[str]) -> str:
        """
        从 IMDb 链接中提取标识；输入为空时返回空字符串。
        """
        if imdb:
            m = re.search(r"tt\d+", imdb)
            if m:
                return m.group(0)
        return ""

    @staticmethod
    def __get_downloadvolumefactor(discount: str) -> float:
        """
        获取下载系数。
        """
        discount_dict = {
            "FREE": 0,
            "PERCENT_50": 0.5,
            "PERCENT_70": 0.3,
            "_2X_FREE": 0,
            "_2X_PERCENT_50": 0.5
        }
        if discount:
            return discount_dict.get(discount, 1)
        return 1

    @staticmethod
    def __get_uploadvolumefactor(discount: str) -> float:
        """
        获取上传系数。
        """
        uploadvolumefactor_dict = {
            "_2X": 2.0,
            "_2X_FREE": 2.0,
            "_2X_PERCENT_50": 2.0
        }
        if discount:
            return uploadvolumefactor_dict.get(discount, 1)
        return 1

    def __get_download_url(self, torrent_id: object) -> str:
        """
        获取下载链接，返回 base64 编码的 JSON 字符串及 URL。

        :param torrent_id: 按站点响应原样写入下载配方的种子 ID
        :return: 当前认证参数和下载地址组成的配方字符串
        """
        url = self._downloadurl % self._api_domain
        params = {
            'method': 'post',
            'cookie': False,
            'params': {
                'id': torrent_id
            },
            'header': {
                'User-Agent': f'{self._ua}',
                'Accept': 'application/json, text/plain, */*',
                'x-api-key': self._apikey
            },
            'proxy': True if self._proxy else False,
            'result': 'data'
        }
        base64_str = base64.b64encode(json.dumps(params).encode('utf-8')).decode('utf-8')
        return f"[{base64_str}]{url}"

    def get_download_url(self, torrent_id: str) -> str:
        """
        根据站内 ID 与当前凭据重建下载配方，不重新搜索种子。

        :param torrent_id: M-Team 分配的种子 ID
        :return: 携带当前认证信息的下载配方，仅供当前请求使用，不应持久化 API Key
        """
        return self.__get_download_url(torrent_id)

    def get_subtitle_links(self, page_url: str) -> List[str]:
        """
        获取指定页面的字幕下载链接。

        :param page_url: 种子详情页网址
        :return: 字幕下载链接
        """
        if not page_url:
            return []
        # 从馒头的详情页网址中提取种子id
        torrent_id = urlparse(page_url).path.rsplit("/", 1)[-1].strip()
        if not torrent_id:
            return []
        return self.get_subtitle_links_by_id(torrent_id)

    def get_subtitle_links_by_id(self, torrent_id: str) -> List[str]:
        """
        获取指定种子的字幕下载链接。

        :param torrent_id: 种子ID
        :return: 字幕下载链接
        """
        results = []
        try:
            for subtitle_id in self.__subtitle_ids(torrent_id) or []:
                if link := self.__subtitle_genlink(subtitle_id):
                    results.append(link)
        except Exception as e:
            logger.error(f"{self._name} 获取字幕失败：{e}")
        return results

    def __subtitle_ids(self, torrent_id: str) -> Optional[List[str]]:
        """
        获取指定种子的字幕列表。

        :param torrent_id: 种子ID
        :return: 字幕ID
        """
        url = self._subtitle_list_url % self._api_domain
        res = RequestUtils(
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey,
            },
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(url, data={"id": torrent_id})
        if res and res.status_code == 200:
            result = res.json()
            if int(result.get("code", -1)) == 0:
                return [item["id"] for item in result.get("data", []) if "id" in item]
            else:
                logger.warn(
                    f'{self._name} 获取字幕列表失败，返回：{result.get("message", "未知")}'
                )
                return None
        elif res is not None:
            logger.warn(f"{self._name} 获取字幕列表失败，错误码：{res.status_code}")
            return None
        else:
            logger.warn(f"{self._name} 获取字幕列表失败，无法连接 {self._domain}")
            return None

    def __subtitle_genlink(self, subtitle_id: str) -> Optional[str]:
        """
        获取字幕下载链接。

        :param subtitle_id: 字幕ID
        :return: 下载链接
        """
        url = self._subtitle_genlink_url % self._api_domain
        res = RequestUtils(
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": f"{self._ua}",
                "x-api-key": self._apikey,
            },
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(url, data={"id": subtitle_id})
        if res and res.status_code == 200:
            result = res.json()
            if int(result.get("code", -1)) == 0 and isinstance(result.get("data"), str):
                return self._subtitle_download_url % (self._api_domain, result["data"])
            else:
                logger.warn(
                    f'{self._name} 获取字幕下载链接失败，返回：{result.get("message", "未知")}'
                )
                return None
        elif res is not None:
            logger.warn(f"{self._name} 获取字幕下载链接失败，错误码：{res.status_code}")
            return None
        else:
            logger.warn(f"{self._name} 获取字幕下载链接失败，无法连接 {self._domain}")
            return None
