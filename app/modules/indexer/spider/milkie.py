from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.foundation import temporal as time_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


class MilkieSpider:
    """Milkie JSON API 种子搜索适配器。"""

    _size = 100
    _timeout = 15
    _proxy: Optional[dict[str, Any]] = None
    _searchurl = "%sapi/v1/torrents"
    _downloadurl = "%sapi/v1/torrents/%s/torrent?key=%s"
    _pageurl = "%sbrowse/%s"

    # 电影分类
    _movie_category = "1"
    # 电视剧分类
    _tv_category = "2"
    # 音乐分类
    _music_category = "3"

    # 站点分类到媒体类型的映射，未覆盖的分类不输出媒体类型
    _category_mapping = {
        1: MediaType.MOVIE,
        2: MediaType.TV,
        3: MediaType.MUSIC,
    }

    def __init__(self, indexer: dict[str, Any]):
        """使用站点配置初始化 Milkie 请求上下文。"""
        if indexer:
            self._indexerid = indexer.get('id')
            self._domain = indexer.get('domain')
            self._searchurl = self._searchurl % self._domain
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = get_runtime_setting('PROXY')
            self._apikey = indexer.get('apikey')
            self._ua = indexer.get('ua')
            self._timeout = indexer.get('timeout') or 15

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量。
        """
        return cls._size

    def __get_params(self, keyword: Optional[str] = None, mtype: Optional[MediaType] = None,
                     page: Optional[int] = 0) -> dict[str, Any]:
        """
        获取搜索参数
        """
        params = {
            "pi": int(page or 0),
            "ps": self._size,
            "oby": "created_at",
            "odir": "desc"
        }
        if keyword:
            params["query"] = keyword
            # 按媒体类型收敛分类，提高关键字搜索的准确度
            if mtype == MediaType.MOVIE:
                params["categories"] = self._movie_category
            elif mtype == MediaType.TV:
                params["categories"] = self._tv_category
            elif mtype == MediaType.MUSIC:
                params["categories"] = self._music_category
        return params

    def __parse_created_at(self, created_at: str) -> str:
        """将站点 ISO8601 发布时间转换为本地日期时间文本。"""
        parsed = time_tools.parse_datetime(created_at)
        if not parsed:
            return created_at or ""
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    def __parse_result(self, results: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """
        解析搜索结果
        """
        torrents: List[dict[str, Any]] = []
        if not results:
            return torrents

        for result in results:
            category = self._category_mapping.get(result.get('category') or 0)
            torrent = {
                'title': result.get('releaseName'),
                'description': result.get('id'),
                # API Key 含 URL 保留字符，必须编码后拼入下载链接
                'enclosure': self._downloadurl % (
                    self._domain, result.get('id'), quote(self._apikey or "", safe="")
                ),
                'pubdate': self.__parse_created_at(result.get('createdAt') or ""),
                'size': result.get('size'),
                'seeders': result.get('seeders'),
                'peers': result.get('leechers'),
                'grabs': result.get('downloaded'),
                # 站点无分享率考核，等效全站免费
                'downloadvolumefactor': 0,
                'uploadvolumefactor': 1,
                'page_url': self._pageurl % (self._domain, result.get('id')),
                'category': category.value if category else None
            }
            externals = result.get('externals') or {}
            if externals.get('imdb'):
                torrent['imdbid'] = externals.get('imdb')
            torrents.append(torrent)

        return torrents

    def __process_response(self, res: Any) -> Tuple[bool, List[dict[str, Any]]]:
        """统一判定搜索响应状态并投影 Milkie 种子结果。"""
        if res and res.status_code == 200:
            results = res.json().get("torrents") or []
            return False, self.__parse_result(results)
        if res is not None:
            if res.status_code in (401, 403):
                logger.warn(f"{self._name} 搜索失败，API Key 无效或已过期")
            else:
                logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
        return True, []

    def __get_headers(self) -> dict[str, str]:
        """构造带 API Key 认证的请求头。"""
        return {
            "x-milkie-auth": self._apikey or "",
            "User-Agent": self._ua or ""
        }

    def search(self, keyword: str, mtype: Optional[MediaType] = None,
               page: Optional[int] = 0) -> Tuple[bool, List[dict[str, Any]]]:
        """
        搜索
        """
        if not self._apikey:
            logger.warn(f"{self._name} 未配置 API Key，无法搜索")
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, mtype, page)

        # 发送请求
        res = RequestUtils(
            headers=self.__get_headers(),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=self._searchurl, params=params)
        return self.__process_response(res)

    async def async_search(self, keyword: str, mtype: Optional[MediaType] = None,
                           page: Optional[int] = 0) -> Tuple[bool, List[dict[str, Any]]]:
        """
        异步搜索
        """
        if not self._apikey:
            logger.warn(f"{self._name} 未配置 API Key，无法搜索")
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, mtype, page)

        # 发送请求
        res = await AsyncRequestUtils(
            headers=self.__get_headers(),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=self._searchurl, params=params)
        return self.__process_response(res)
