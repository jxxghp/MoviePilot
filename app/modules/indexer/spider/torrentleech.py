from typing import List, Tuple
from urllib.parse import quote

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class TorrentLeech:
    _indexer = None
    _proxy = None
    _size = 100
    _searchurl = "%storrents/browse/list/query/%s"
    _browseurl = "%storrents/browse/list/page/2%s"
    _downloadurl = "%sdownload/%s/%s"
    _pageurl = "%storrent/%s"
    _timeout = 15

    def __init__(self, indexer: CommentedMap):
        self._indexer = indexer
        if indexer.get('proxy'):
            self._proxy = settings.PROXY
            self._timeout = indexer.get('timeout') or 15

    def search(self, keyword: str, page: int = 0) -> Tuple[bool, List[dict]]:

        if StringUtils.is_chinese(keyword):
            # 不支持中文
            return True, []

        if keyword:
            url = self._searchurl % (self._indexer.get('domain'), quote(keyword))
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
        torrents = []
        if res and res.status_code == 200:
            results = res.json().get('torrentList') or []
            for result in results:
                torrent = {
                    'title': result.get('name'),
                    'enclosure': self._downloadurl % (self._indexer.get('domain'), result.get('fid'), result.get('filename')),
                    'pubdate': StringUtils.format_timestamp(result.get('addedTimestamp')),
                    'size': result.get('size'),
                    'seeders': result.get('seeders'),
                    'peers': result.get('leechers'),
                    'grabs': result.get('completed'),
                    'downloadvolumefactor': result.get('download_multiplier'),
                    'uploadvolumefactor': 1,
                    'page_url': self._pageurl % (self._indexer.get('domain'), result.get('fid')),
                    'imdbid': result.get('imdbID')
                }
                torrents.append(torrent)
        elif res is not None:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._indexer.get('name')} 搜索失败，无法连接 {self._indexer.get('domain')}")
            return True, []

        return False, torrents
