import base64
import json
import re
from typing import Tuple, List, Optional
from urllib.parse import urlparse

from app.runtime.config import settings
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.log import logger
from app.schemas.types import MediaType
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.domain import site as site_rules
from app.foundation import temporal as time_tools


class MTorrentSpider:
    """
    mTorrent API
    """
    _indexerid = None
    _domain = None
    _url = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _size = 100
    _searchurl = "https://api.%s/api/torrent/search"
    _downloadurl = "https://api.%s/api/torrent/genDlToken"
    _subtitle_list_url = "https://api.%s/api/subtitle/list"
    _subtitle_genlink_url = "https://api.%s/api/subtitle/genlink"
    _subtitle_download_url ="https://api.%s/api/subtitle/dlV2?credential=%s"
    _pageurl = "%sdetail/%s"
    _timeout = 15

    # 电影分类
    _movie_category = ['401', '419', '420', '421', '439', '405', '404']
    # 电视剧分类
    _tv_category = ['403', '402', '435', '438', '404', '405']
    # 音乐分类：434 为无损音乐，406 为演唱分区
    _music_category = ['434', '406']

    # API KEY
    _apikey = None
    # JWT Token
    _token = None

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
        """
        return cls._size

    def __init__(self, indexer: dict):
        self.systemconfig = SystemConfigOper()
        if indexer:
            self._indexerid = indexer.get('id')
            self._url = indexer.get('domain')
            self._domain = site_rules.extract_domain(self._url)
            self._searchurl = self._searchurl % self._domain
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = settings.PROXY
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._apikey = indexer.get('apikey')
            self._token = indexer.get('token')
            self._timeout = indexer.get('timeout') or 15

    def __get_params(self, keyword: str, mtype: MediaType = None, page: Optional[int] = 0) -> dict:
        """
        获取请求参数
        """
        if not mtype:
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
            "pageNumber": int(page) + 1,
            "pageSize": self._size,
            "visible": 1
        }

    def __parse_result(self, results: List[dict]):
        """
        解析搜索结果
        """
        torrents = []
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

    def search(self, keyword: str, mtype: MediaType = None, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        搜索
        """
        # 检查ApiKey
        if not self._apikey:
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, mtype, page)

        # 发送请求
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
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("data") or []
            return False, self.__parse_result(results)
        elif res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
            return True, []

    async def async_search(self, keyword: str, mtype: MediaType = None, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        搜索
        """
        # 检查ApiKey
        if not self._apikey:
            return True, []

        # 获取请求参数
        params = self.__get_params(keyword, mtype, page)

        # 发送请求
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
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("data") or []
            return False, self.__parse_result(results)
        elif res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
            return True, []

    @staticmethod
    def __find_imdbid(imdb: str) -> str:
        """
        从imdb链接中提取imdbid
        """
        if imdb:
            m = re.search(r"tt\d+", imdb)
            if m:
                return m.group(0)
        return ""

    @staticmethod
    def __get_downloadvolumefactor(discount: str) -> float:
        """
        获取下载系数
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
        获取上传系数
        """
        uploadvolumefactor_dict = {
            "_2X": 2.0,
            "_2X_FREE": 2.0,
            "_2X_PERCENT_50": 2.0
        }
        if discount:
            return uploadvolumefactor_dict.get(discount, 1)
        return 1

    def __get_download_url(self, torrent_id: str) -> str:
        """
        获取下载链接，返回base64编码的json字符串及URL
        """
        url = self._downloadurl % self._domain
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
        # base64编码
        base64_str = base64.b64encode(json.dumps(params).encode('utf-8')).decode('utf-8')
        return f"[{base64_str}]{url}"

    def get_subtitle_links(self, page_url: str) -> List[str]:
        """
        获取指定页面的字幕下载链接

        :param page_url: 种子详情页网址
        :type page_url: str
        :return: 字幕下载链接
        :rtype: List[str]
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
        获取指定种子的字幕下载链接

        :param torrent_id: 种子ID
        :type torrent_id: str
        :return: 字幕下载链接
        :rtype: List[str]
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
        获取指定种子的字幕列表

        :param torrent_id: 种子ID
        :type torrent_id: str
        :return: 字幕ID
        :rtype: List[str] | None
        """
        url = self._subtitle_list_url % self._domain
        # 发送请求
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
        获取字幕下载链接

        :param subtitle_id: 字幕ID
        :type subtitle_id: str
        :return: 下载链接
        :rtype: str | None
        """
        url = self._subtitle_genlink_url % self._domain
        # 发送请求
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
                return self._subtitle_download_url % (self._domain, result["data"])
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
