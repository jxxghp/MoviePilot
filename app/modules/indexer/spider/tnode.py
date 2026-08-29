import re
from typing import Any, Tuple, List, Optional

from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting

from app.runtime.log import logger
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.foundation.singleton import SingletonClass
from app.foundation import temporal as time_tools


class TNodeSpider(metaclass=SingletonClass):
    """TNode 页面令牌与种子搜索 API 适配器。"""

    _size = 100
    _timeout = 15
    _proxy = None
    _baseurl = "%sapi/torrent/advancedSearch"
    _downloadurl = "%sapi/torrent/download/%s"
    _pageurl = "%storrent/info/%s"

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量。
        """
        return cls._size

    def __init__(self, indexer: dict):
        """使用站点配置初始化 TNode 请求上下文。"""
        if indexer:
            self._indexerid = indexer.get('id')
            self._domain = indexer.get('domain')
            self._searchurl = self._baseurl % self._domain
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = get_runtime_setting('PROXY')
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._timeout = indexer.get('timeout') or 15

    @cached(region="indexer_spider", maxsize=1, ttl=60 * 60 * 24, skip_empty=True, shared_key="get_token")
    def __get_token(self) -> Optional[str]:
        """同步请求站点首页并提取 CSRF 令牌。"""
        if not self._domain:
            return
        res = RequestUtils(ua=self._ua,
                           cookies=self._cookie,
                           proxies=self._proxy,
                           timeout=self._timeout).get_res(url=self._domain)
        return self.__parse_token_response(res)

    @cached(region="indexer_spider", maxsize=1, ttl=60 * 60 * 24, skip_empty=True, shared_key="get_token")
    async def __async_get_token(self) -> Optional[str]:
        """异步请求站点首页并提取 CSRF 令牌。"""
        if not self._domain:
            return
        res = await AsyncRequestUtils(ua=self._ua,
                                      cookies=self._cookie,
                                      proxies=self._proxy,
                                      timeout=self._timeout).get_res(url=self._domain)
        return self.__parse_token_response(res)

    @staticmethod
    def __parse_token_response(res: Any) -> Optional[str]:
        """从同步或异步首页响应中提取有效 CSRF 令牌。"""
        if not res or res.status_code != 200:
            return None
        csrf_token = re.search(r'<meta name="x-csrf-token" content="(.+?)">', res.text)
        return csrf_token.group(1) if csrf_token else None

    def __get_params(self, keyword: str = None, page: Optional[int] = 0) -> dict:
        """
        获取搜索参数
        """
        search_type = "imdbid" if (keyword and keyword.startswith('tt')) else "title"
        return {
            "page": int(page) + 1,
            "size": self._size,
            "type": search_type,
            "keyword": keyword or "",
            "sorter": "id",
            "order": "desc",
            "tags": [],
            "category": [501, 502, 503, 504],
            "medium": [],
            "videoCoding": [],
            "audioCoding": [],
            "resolution": [],
            "group": []
        }

    def __parse_result(self, results: List[dict]) -> List[dict]:
        """
        解析搜索结果
        """
        torrents = []
        if not results:
            return torrents

        for result in results:
            torrent = {
                'title': result.get('title'),
                'description': result.get('subtitle'),
                'enclosure': self._downloadurl % (self._domain, result.get('id')),
                'pubdate': time_tools.format_timestamp(result.get('upload_time')),
                'size': result.get('size'),
                'seeders': result.get('seeding'),
                'peers': result.get('leeching'),
                'grabs': result.get('complete'),
                'downloadvolumefactor': result.get('downloadRate'),
                'uploadvolumefactor': result.get('uploadRate'),
                'page_url': self._pageurl % (self._domain, result.get('id')),
                'imdbid': result.get('imdb')
            }
            torrents.append(torrent)

        return torrents

    def __process_response(self, res: Any) -> Tuple[bool, List[dict[str, Any]]]:
        """统一判定搜索响应状态并投影 TNode 种子结果。"""
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("torrents") or []
            return False, self.__parse_result(results)
        if res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
        return True, []

    def search(self, keyword: str, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        搜索
        """
        # 获取token
        _token = self.__get_token()
        if not _token:
            logger.warn(f"{self._name} 未获取到token，无法搜索")
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, page)

        # 发送请求
        res = RequestUtils(
            headers={
                'X-CSRF-TOKEN': _token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._ua}"
            },
            cookies=self._cookie,
            proxies=self._proxy,
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        return self.__process_response(res)
        
    async def async_search(self, keyword: str, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        异步搜索
        """
        # 获取token
        _token = await self.__async_get_token()
        if not _token:
            logger.warn(f"{self._name} 未获取到token，无法搜索")
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, page)

        # 发送请求
        res = await AsyncRequestUtils(
            headers={
                'x-csrf-token': _token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._ua}"
            },
            cookies=self._cookie,
            proxies=self._proxy,
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        return self.__process_response(res)
