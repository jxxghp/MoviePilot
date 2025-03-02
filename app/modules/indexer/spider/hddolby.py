from typing import Tuple, List

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class HddolbySpider:
    """
    HDDolby API
    """
    _indexerid = None
    _domain = None
    _domain_host = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _apikey = None
    _size = 40
    _pageurl = None
    _timeout = 15
    _searchurl = None

    # 分类
    _movie_category = [401, 405]
    _tv_category = [402, 403, 404, 405]

    # 标签
    _labels = {
        "gf": "官方",
        "gy": "国语",
        "yy": "粤语",
        "ja": "日语",
        "ko": "韩语",
        "zz": "中文字幕",
        "jz": "禁转",
        "xz": "限转",
        "diy": "DIY",
        "sf": "首发",
        "yq": "应求",
        "m0": "零魔",
        "yc": "原创",
        "gz": "官字",
        "db": "Dolby Vision",
        "hdr10": "HDR10",
        "hdrm": "HDR10+",
        "tx": "特效",
        "lz": "连载",
        "wj": "完结",
        "hdrv": "HDR Vivid",
        "hlg": "HLG",
        "hq": "高码率",
        "hfr": "高帧率",
    }

    def __init__(self, indexer: dict):
        self.systemconfig = SystemConfigOper()
        if indexer:
            self._indexerid = indexer.get('id')
            self._domain = indexer.get('domain')
            self._domain_host = StringUtils.get_url_domain(self._domain)
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = settings.PROXY
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._apikey = indexer.get('apikey')
            self._timeout = indexer.get('timeout') or 15
            self._searchurl = f"https://api.{self._domain_host}/api/v1/torrent/search"
            self._pageurl = f"{self._domain}details.php?id=%s&hit=1"

    def search(self, keyword: str, mtype: MediaType = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """
        搜索
        """

        if mtype == MediaType.TV:
            categories = self._tv_category
        elif mtype == MediaType.MOVIE:
            categories = self._movie_category
        else:
            categories = list(set(self._movie_category + self._tv_category))

        # 输入参数
        params = {
            "keyword": keyword,
            "page_number": page,
            "page_size": 100,
            "categories": categories,
            "visible": 1,
        }

        res = RequestUtils(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "x-api-key": self._apikey
            },
            cookies=self._cookie,
            proxies=self._proxy,
            referer=f"{self._domain}",
            timeout=self._timeout
        ).post_res(url=self._searchurl, json=params)
        torrents = []
        if res and res.status_code == 200:
            results = res.json().get('data', []) or []
            for result in results:
                """
                {
                    "id": 120202,
                    "promotion_time_type": 0,
                    "promotion_until": "0000-00-00 00:00:00",
                    "category": 402,
                    "medium": 6,
                    "codec": 1,
                    "standard": 2,
                    "team": 10,
                    "audiocodec": 14,
                    "leechers": 0,
                    "seeders": 1,
                    "name": "[DBY] Lost S06 2010 Complete 1080p Netflix WEB-DL AVC DDP5.1-DBTV",
                    "small_descr": "lost ",
                    "times_completed": 0,
                    "size": 33665425886,
                    "added": "2025-02-18 19:47:56",
                    "url": 0,
                    "hr": 0,
                    "tmdb_type": "tv",
                    "tmdb_id": 4607,
                    "imdb_id": null,
                    "tags": "gf"
                }
                """
                # 类别
                category_value = result.get('category')
                if category_value in self._tv_category:
                    category = MediaType.TV.value
                elif category_value in self._movie_category:
                    category = MediaType.MOVIE.value
                else:
                    category = MediaType.UNKNOWN.value
                # 标签
                torrentLabelIds = result.get('tags', "").split(";") or []
                torrentLabels = []
                for labelId in torrentLabelIds:
                    if self._labels.get(labelId) is not None:
                        torrentLabels.append(self._labels.get(labelId))
                # 种子信息
                torrent = {
                    'title': result.get('name'),
                    'description': result.get('small_descr'),
                    'enclosure': self.__get_download_url(result.get('id'), result.get('downhash')),
                    'pubdate': result.get('added'),
                    'size': result.get('size'),
                    'seeders': result.get('seeders'),
                    'peers': result.get('leechers'),
                    'grabs': result.get('times_completed'),
                    'downloadvolumefactor': self.__get_downloadvolumefactor(result.get('promotion_time_type')),
                    'uploadvolumefactor': self.__get_uploadvolumefactor(result.get('promotion_time_type')),
                    'freedate': result.get('promotion_until'),
                    'page_url': self._pageurl % (self._domain, result.get('id')),
                    'labels': torrentLabels,
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

    @staticmethod
    def __get_downloadvolumefactor(discount: int) -> float:
        """
        获取下载系数
        """
        discount_dict = {
            2: 0,
            5: 0.5,
            6: 1,
            7: 0.3
        }
        if discount:
            return discount_dict.get(discount, 1)
        return 1

    @staticmethod
    def __get_uploadvolumefactor(discount: int) -> float:
        """
        获取上传系数
        """
        discount_dict = {
            3: 2,
            4: 2,
            6: 2
        }
        if discount:
            return discount_dict.get(discount, 1)
        return 1

    def __get_download_url(self, torrent_id: int, downhash: str) -> str:
        """
        获取下载链接，返回base64编码的json字符串及URL
        """
        return f"{self._domain}download.php?id={torrent_id}&downhash={downhash}"
