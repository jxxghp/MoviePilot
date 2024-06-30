import urllib.parse
from typing import Tuple, List

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class HaiDanSpider:
    """
    haidan.video API
    """
    _indexerid = None
    _domain = None
    _url = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _size = 100
    _searchurl = "%storrents.php"
    _detailurl = "%sdetails.php?group_id=%s&torrent_id=%s"
    _timeout = 15

    # 电影分类
    _movie_category = ['401', '404', '405']
    _tv_category = ['402', '403', '404', '405']

    # 足销状态 1-普通，2-免费，3-2X，4-2X免费，5-50%，6-2X50%，7-30%
    _dl_state = {
        "1": 1,
        "2": 0,
        "3": 1,
        "4": 0,
        "5": 0.5,
        "6": 0.5,
        "7": 0.3
    }
    _up_state = {
        "1": 1,
        "2": 1,
        "3": 2,
        "4": 2,
        "5": 1,
        "6": 2,
        "7": 1
    }

    def __init__(self, indexer: CommentedMap):
        self.systemconfig = SystemConfigOper()
        if indexer:
            self._indexerid = indexer.get('id')
            self._url = indexer.get('domain')
            self._domain = StringUtils.get_url_domain(self._url)
            self._searchurl = self._searchurl % self._url
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = settings.PROXY
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._timeout = indexer.get('timeout') or 15

    def search(self, keyword: str, mtype: MediaType = None) -> Tuple[bool, List[dict]]:
        """
        搜索
        """

        def __dict_to_query(_params: dict):
            """
            将数组转换为逗号分隔的字符串
            """
            for key, value in _params.items():
                if isinstance(value, list):
                    _params[key] = ','.join(map(str, value))
            return urllib.parse.urlencode(params)

        # 检查cookie
        if not self._cookie:
            return True, []

        if not mtype:
            categories = []
        elif mtype == MediaType.TV:
            categories = self._tv_category
        else:
            categories = self._movie_category

        # 搜索类型
        if keyword.startswith('tt'):
            search_area = '4'
        else:
            search_area = '0'

        params = {
            "isapi": "1",
            "search_area": search_area,  # 0-标题 1-简介（较慢）3-发种用户名 4-IMDb
            "search": keyword,
            "search_mode": "0",  # 0-与 1-或 2-精准
            "cat": categories
        }
        res = RequestUtils(
            cookies=self._cookie,
            ua=self._ua,
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=f"{self._searchurl}?{__dict_to_query(params)}")
        torrents = []
        if res and res.status_code == 200:
            result = res.json()
            code = result.get('code')
            if code != 0:
                logger.warn(f"{self._name} 搜索失败：{result.get('msg')}")
                return True, []
            data = result.get('data') or {}
            for tid, item in data.items():
                category_value = result.get('category')
                if category_value in self._tv_category \
                        and category_value not in self._movie_category:
                    category = MediaType.TV.value
                elif category_value in self._movie_category:
                    category = MediaType.MOVIE.value
                else:
                    category = MediaType.UNKNOWN.value
                torrent = {
                    'title': item.get('name'),
                    'description': item.get('small_descr'),
                    'enclosure': item.get('url'),
                    'pubdate': StringUtils.format_timestamp(item.get('added')),
                    'size': int(item.get('size') or '0'),
                    'seeders': int(item.get('seeders') or '0'),
                    'peers': int(item.get("leechers") or '0'),
                    'grabs': int(item.get("times_completed") or '0'),
                    'downloadvolumefactor': self.__get_downloadvolumefactor(item.get('sp_state')),
                    'uploadvolumefactor': self.__get_uploadvolumefactor(item.get('sp_state')),
                    'page_url': self._detailurl % (self._url, item.get('group_id'), tid),
                    'labels': [],
                    'category': category
                }
                torrents.append(torrent)
        elif res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
            return True, []
        return False, torrents

    def __get_downloadvolumefactor(self, discount: str) -> float:
        """
        获取下载系数
        """
        if discount:
            return self._dl_state.get(discount, 1)
        return 1

    def __get_uploadvolumefactor(self, discount: str) -> float:
        """
        获取上传系数
        """
        if discount:
            return self._up_state.get(discount, 1)
        return 1
