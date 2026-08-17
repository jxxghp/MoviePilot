from typing import List, Tuple, Optional
from urllib.parse import quote

from app.runtime.config import settings
from app.runtime.log import logger
from app.schemas.types import MediaType
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools


class TorrentLeech:
    """TorrentLeech JSON 搜索接口索引器。"""

    _indexer = None
    _proxy = None
    _size = 100
    _searchurl = "%storrents/browse/list/query/%s"
    _browseurl = "%storrents/browse/list/page/%s"
    _downloadurl = "%sdownload/%s/%s"
    _pageurl = "%storrent/%s"
    _timeout = 15

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量；关键词搜索 URL 当前没有可靠页码入口。
        """
        return None if keyword else cls._size

    def __init__(self, indexer: dict):
        """初始化站点认证信息和媒体分类配置。"""
        self._indexer = indexer
        if indexer.get('proxy'):
            self._proxy = settings.PROXY
            self._timeout = indexer.get('timeout') or 15

    def __category_ids(self, mtype: MediaType = None) -> List[str]:
        """读取资源包中与请求媒体类型对应的 TorrentLeech 分类 ID。"""
        category_key = {
            MediaType.MOVIE: "movie",
            MediaType.TV: "tv",
            MediaType.MUSIC: "music",
        }.get(mtype)
        if not category_key:
            return []
        return [
            str(item.get("id"))
            for item in ((self._indexer.get("category") or {}).get(category_key) or [])
            if isinstance(item, dict) and item.get("id") is not None
        ]

    def __get_search_url(self, keyword: str, mtype: MediaType = None) -> str:
        """按媒体类型构造 TorrentLeech 搜索接口地址。"""
        domain = self._indexer.get('domain')
        encoded_keyword = quote(keyword)
        category_ids = self.__category_ids(mtype)
        if category_ids:
            return (
                f"{domain}torrents/browse/list/categories/{','.join(category_ids)}"
                f"/exact/1/query/{encoded_keyword}"
            )
        return self._searchurl % (domain, encoded_keyword)

    def __parse_result(self, results: List[dict], mtype: MediaType = None) -> List[dict]:
        """
        解析搜索结果
        """
        torrents = []
        if not results:
            return torrents

        requested_category_ids = set(self.__category_ids(mtype))
        for result in results:
            torrent = {
                'title': result.get('name'),
                'enclosure': self._downloadurl % (self._indexer.get('domain'),
                                                  result.get('fid'),
                                                  result.get('filename')),
                'pubdate': time_tools.format_timestamp(result.get('addedTimestamp')),
                'size': result.get('size'),
                'seeders': result.get('seeders'),
                'peers': result.get('leechers'),
                'grabs': result.get('completed'),
                'downloadvolumefactor': result.get('download_multiplier'),
                'uploadvolumefactor': 1,
                'page_url': self._pageurl % (self._indexer.get('domain'), result.get('fid')),
                'imdbid': result.get('imdbID')
            }
            if requested_category_ids:
                torrent['category'] = (
                    mtype.value
                    if str(result.get('categoryID')) in requested_category_ids
                    else MediaType.UNKNOWN.value
                )
            torrents.append(torrent)
        return torrents

    def search(
        self,
        keyword: str,
        mtype: MediaType = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        搜索种子
        """
        if text_tools.contains_chinese(keyword):
            # 不支持中文
            return True, []

        if keyword:
            url = self.__get_search_url(keyword, mtype)
        else:
            url = self._browseurl % (self._indexer.get('domain'), int(page) + 1)

        res = RequestUtils(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._indexer.get('ua')}",
            },
            cookies=self._indexer.get('cookie'),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url)
        if res and res.status_code == 200:
            results = res.json().get('torrentList') or []
            return False, self.__parse_result(results, mtype)
        elif res is not None:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，无法连接 {self._indexer.get('domain')}")
            return True, []

    async def async_search(
        self,
        keyword: str,
        mtype: MediaType = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索种子
        """
        if text_tools.contains_chinese(keyword):
            # 不支持中文
            return True, []

        if keyword:
            url = self.__get_search_url(keyword, mtype)
        else:
            url = self._browseurl % (self._indexer.get('domain'), int(page) + 1)

        res = await AsyncRequestUtils(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._indexer.get('ua')}",
            },
            cookies=self._indexer.get('cookie'),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url)
        if res and res.status_code == 200:
            results = res.json().get('torrentList') or []
            return False, self.__parse_result(results, mtype)
        elif res is not None:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，无法连接 {self._indexer.get('domain')}")
            return True, []
