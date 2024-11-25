from typing import Tuple, List

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class YemaSpider:
    """
    YemaPT API
    """
    _indexerid = None
    _domain = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _size = 40
    _searchurl = "%sapi/torrent/fetchOpenTorrentList"
    _downloadurl = "%sapi/torrent/download?id=%s"
    _pageurl = "%s#/torrent/detail/%s/"
    _timeout = 15

    # 分类
    _movie_category = [4]
    _tv_category = [5, 13, 14, 17, 15, 6, 16]

    # 标签 https://wiki.yemapt.org/developer/constants
    _labels = {
        "1": "禁转",
        "2": "首发",
        "3": "官方",
        "4": "自制",
        "5": "国语",
        "6": "中字",
        "7": "粤语",
        "8": "英字",
        "9": "HDR10",
        "10": "杜比视界",
        "11": "分集",
        "12": "完结",
    }

    def __init__(self, indexer: CommentedMap):
        self.systemconfig = SystemConfigOper()
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

    def search(self, keyword: str, mtype: MediaType = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """
        搜索
        """
        params = {
            "pageParam": {
                "current": page + 1,
                "pageSize": self._size,
                "total": self._size
            },
            "sorter": {}
        }
        # 新接口可不传 categoryId 参数
        # if mtype == MediaType.MOVIE:
        #     params.update({
        #         "categoryId": self._movie_category,
        #     })
        #     pass
        if keyword:
            params.update({
                "keyword": keyword,
            })
        res = RequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{self._ua}",
                "Accept": "application/json, text/plain, */*"
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
                category_value = result.get('categoryId')
                if category_value in self._tv_category :
                    category = MediaType.TV.value
                elif category_value in self._movie_category:
                    category = MediaType.MOVIE.value
                else:
                    category = MediaType.UNKNOWN.value
                    pass

                torrentLabelIds = result.get('tagList', []) or []
                torrentLabels = []
                for labelId in torrentLabelIds:
                    if self._labels.get(labelId) is not None:
                        torrentLabels.append(self._labels.get(labelId))
                        pass
                    pass
                torrent = {
                    'title': result.get('showName'),
                    'description': result.get('shortDesc'),
                    'enclosure': self.__get_download_url(result.get('id')),
                    # 使用上架时间，而不是用户发布时间，上架时间即其他用户可见时间
                    'pubdate': StringUtils.unify_datetime_str(result.get('listingTime')),
                    'size': result.get('fileSize'),
                    'seeders': result.get('seedNum'),
                    'peers': result.get('leechNum'),
                    'grabs': result.get('completedNum'),
                    'downloadvolumefactor': self.__get_downloadvolumefactor(result.get('downloadPromotion')),
                    'uploadvolumefactor': self.__get_uploadvolumefactor(result.get('uploadPromotion')),
                    'freedate': StringUtils.unify_datetime_str(result.get('downloadPromotionEndTime')),
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
    def __get_downloadvolumefactor(discount: str) -> float:
        """
        获取下载系数
        """
        discount_dict = {
            "free": 0,
            "half": 0.5,
            "none": 1
        }
        if discount:
            return discount_dict.get(discount, 1)
        return 1

    @staticmethod
    def __get_uploadvolumefactor(discount: str) -> float:
        """
        获取上传系数
        """
        discount_dict = {
            "none": 1,
            "one_half": 1.5,
            "double_upload": 2
        }
        if discount:
            return discount_dict.get(discount, 1)
        return 1

    def __get_download_url(self, torrent_id: str) -> str:
        """
        获取下载链接
        """
        return self._downloadurl % (self._domain, torrent_id)
