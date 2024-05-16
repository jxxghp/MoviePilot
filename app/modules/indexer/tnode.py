import re
from typing import Tuple, List

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class TNodeSpider:
    _indexerid = None
    _domain = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _token = None
    _size = 100
    _timeout = 15
    _searchurl = "%sapi/torrent/advancedSearch"
    _downloadurl = "%sapi/torrent/download/%s"
    _pageurl = "%storrent/info/%s"

    def __init__(self, indexer: CommentedMap):
        if indexer:
            self._indexerid = indexer.get('id')
            self._domain = indexer.get('domain')
            self._searchurl = self._searchurl % self._domain
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = settings.PROXY
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._timeout = indexer.get('timeout') or 15
        self.init_config()

    def init_config(self):
        self.__get_token()

    def __get_token(self):
        if not self._domain:
            return
        res = RequestUtils(ua=self._ua,
                           cookies=self._cookie,
                           proxies=self._proxy,
                           timeout=self._timeout).get_res(url=self._domain)
        if res and res.status_code == 200:
            csrf_token = re.search(r'<meta name="x-csrf-token" content="(.+?)">', res.text)
            if csrf_token:
                self._token = csrf_token.group(1)

    def search(self, keyword: str, page: int = 0) -> Tuple[bool, List[dict]]:
        if not self._token:
            logger.warn(f"{self._name} 未获取到token，无法搜索")
            return True, []
        search_type = "imdbid" if (keyword and keyword.startswith('tt')) else "title"
        params = {
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
        res = RequestUtils(
            headers={
                'X-CSRF-TOKEN': self._token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{self._ua}"
            },
            cookies=self._cookie,
            proxies=self._proxy,
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        torrents = []
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("torrents") or []
            for result in results:
                torrent = {
                    'title': result.get('title'),
                    'description': result.get('subtitle'),
                    'enclosure': self._downloadurl % (self._domain, result.get('id')),
                    'pubdate': StringUtils.format_timestamp(result.get('upload_time')),
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
        elif res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
            return True, []
        return False, torrents
