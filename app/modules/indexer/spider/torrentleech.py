"""
通过 TorrentLeech 接口搜索种子并转换为通用索引结果。
"""

from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


class TorrentLeech:
    """
    TorrentLeech JSON 搜索接口索引器。
    """

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
        获取 TorrentLeech 当前搜索入口的分页容量。

        :param keyword: 有关键词时使用搜索 URL，无关键词时使用浏览 URL
        :return: 浏览接口的单页数量；关键词搜索没有可靠页码入口，返回 None
        """
        return None if keyword else cls._size

    def __init__(self, indexer: dict[str, Any]):
        """
        初始化站点认证信息和媒体分类配置。
        """
        self._indexer = indexer
        if indexer.get('proxy'):
            self._proxy = get_runtime_setting('PROXY')
            self._timeout = indexer.get('timeout') or 15

    def __category_ids(self, mtype: Optional[MediaType] = None) -> List[str]:
        """
        读取资源包中与请求媒体类型对应的 TorrentLeech 分类 ID。
        """
        if mtype is None:
            return []
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

    def __get_search_url(self, keyword: str, mtype: Optional[MediaType] = None) -> str:
        """
        按媒体类型构造 TorrentLeech 搜索接口地址。
        """
        domain = self._indexer.get('domain')
        encoded_keyword = quote(keyword)
        category_ids = self.__category_ids(mtype)
        if category_ids:
            return (
                f"{domain}torrents/browse/list/categories/{','.join(category_ids)}"
                f"/exact/1/query/{encoded_keyword}"
            )
        return self._searchurl % (domain, encoded_keyword)

    def __parse_result(
        self,
        results: List[dict[str, Any]],
        mtype: Optional[MediaType] = None,
    ) -> List[dict[str, Any]]:
        """
        解析搜索结果。
        """
        torrents: list[dict[str, object]] = []
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
            if requested_category_ids and mtype is not None:
                torrent['category'] = (
                    mtype.value
                    if str(result.get('categoryID')) in requested_category_ids
                    else MediaType.UNKNOWN.value
                )
            torrents.append(torrent)
        return torrents

    def __process_response(
        self,
        res: Any,
        mtype: Any = None,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """
        统一判定搜索响应状态并按请求媒体类型投影 TorrentLeech 结果。
        """
        if res and res.status_code == 200:
            results = res.json().get('torrentList') or []
            return False, self.__parse_result(results, mtype)
        if res is not None:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，错误码：{res.status_code}")
            return True, []
        logger.warn(
            f"{self._indexer.get('name')} 搜索失败，无法连接 {self._indexer.get('domain')}"
        )
        return True, []

    def search(
        self,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """
        同步搜索 TorrentLeech 种子。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :param page: 从 0 开始的搜索页码
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if text_tools.contains_chinese(keyword):
            # 不支持中文
            return True, []

        if keyword:
            url = self.__get_search_url(keyword, mtype)
        else:
            url = self._browseurl % (self._indexer.get('domain'), int(page or 0) + 1)

        res = RequestUtils(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._indexer.get('ua')}",
            },
            cookies=self._indexer.get('cookie'),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url)
        return self.__process_response(res, mtype)

    async def async_search(
        self,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """
        异步搜索 TorrentLeech 种子。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :param page: 从 0 开始的搜索页码
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if text_tools.contains_chinese(keyword):
            # 不支持中文
            return True, []

        if keyword:
            url = self.__get_search_url(keyword, mtype)
        else:
            url = self._browseurl % (self._indexer.get('domain'), int(page or 0) + 1)

        res = await AsyncRequestUtils(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._indexer.get('ua')}",
            },
            cookies=self._indexer.get('cookie'),
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url)
        return self.__process_response(res, mtype)
