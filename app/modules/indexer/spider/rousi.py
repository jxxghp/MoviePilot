import base64
import json
from typing import List, Optional, Tuple

from app.runtime.config import settings
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.log import logger
from app.schemas.types import MediaType
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.domain import site as site_rules
from app.foundation import temporal as time_tools


class RousiSpider:
    """
    Rousi.pro API v1 Spider

    使用 API v1 接口进行种子搜索
    - 认证方式：Bearer Token (Passkey)
    - 搜索接口：/api/v1/torrents
    - 详情接口：/api/v1/torrents/:id
    """
    _indexerid = None
    _domain = None
    _url = None
    _name = ""
    _proxy = None
    _cookie = None
    _ua = None
    _size = 100
    _searchurl = "https://%s/api/v1/torrents"
    _downloadurl = "https://%s/api/v1/torrents/%s"
    _timeout = 15

    # 分类定义
    # API 不支持多分类搜索，每次只使用一个分类
    _movie_category = 'movie'
    _tv_category = 'tv'
    _music_category = 'music'

    # API KEY
    _apikey = None

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
            self._downloadurl = self._downloadurl % (self._domain, "%s")
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = settings.PROXY
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._apikey = indexer.get('apikey')
            self._timeout = indexer.get('timeout') or 15

    def __get_params(self, keyword: str, mtype: MediaType = None, cat: Optional[str] = None, page: Optional[int] = 0) -> dict:
        """
        构建 API 请求参数

        :param keyword: 搜索关键词
        :param mtype: 媒体类型 (MOVIE/TV/MUSIC)
        :param cat: 用户选择的分类 ID（逗号分隔的字符串）
        :param page: 页码（从 0 开始，API 需要从 1 开始）
        :return: 请求参数字典
        """
        params = {
            "page": int(page) + 1,
            "page_size": self._size
        }
        if keyword:
            params["keyword"] = keyword

        # API 不支持多分类搜索,只使用单个 category 参数
        # 优先使用用户选择的分类,如果用户未选择则根据 mtype 推断
        if cat:
            # 用户选择了特定分类,需要将分类 ID 映射回 API 的 category name
            category_names = self.__get_category_names_by_ids(cat)
            if category_names:
                # 如果用户选择了多个分类,只取第一个
                params["category"] = category_names[0]
        elif mtype:
            # 用户未选择分类,根据媒体类型推断
            if mtype == MediaType.MOVIE:
                params["category"] = self._movie_category
            elif mtype == MediaType.TV:
                params["category"] = self._tv_category
            elif mtype == MediaType.MUSIC:
                params["category"] = self._music_category

        return params

    def __get_category_names_by_ids(self, cat: str) -> Optional[list]:
        """
        根据用户选择的分类 ID 获取 API 的 category names

        :param cat: 用户选择的分类 ID（逗号分隔的多个ID，如 "1,2,3"）
        :return: API 的 category names 列表（如 ["movie", "tv", "documentary"]）
        """
        if not cat:
            return None

        # ID 到 category name 的映射
        id_to_name = {
            '1': 'movie',
            '2': 'tv',
            '3': 'documentary',
            '4': 'animation',
            '5': 'music',
            '6': 'variety'
        }

        # 分割多个分类 ID 并映射为 category names
        cat_ids = [c.strip() for c in cat.split(',') if c.strip()]
        category_names = [id_to_name.get(cat_id) for cat_id in cat_ids if cat_id in id_to_name]

        return category_names if category_names else None

    def __process_response(self, res) -> Tuple[bool, List[dict]]:
        """
        处理 API 响应

        :param res: 请求响应对象
        :return: (是否发生错误, 种子列表)
        """
        if res and res.status_code == 200:
            try:
                data = res.json()
                if data.get('code') == 0:
                    results = data.get('data', {}).get('torrents', [])
                    return False, self.__parse_result(results)
                else:
                    logger.warn(f"{self._name} 搜索失败，错误信息：{data.get('message')}")
                    return True, []
            except Exception as e:
                logger.warn(f"{self._name} 解析响应失败：{e}")
                return True, []
        elif res is not None:
            logger.warn(f"{self._name} 搜索失败，HTTP 错误码：{res.status_code}")
            return True, []
        else:
            logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
            return True, []

    def __parse_result(self, results: List[dict]) -> List[dict]:
        """
        解析搜索结果

        将 API 返回的种子数据转换为 MoviePilot 标准格式

        :param results: API 返回的种子列表
        :return: 标准化的种子信息列表
        """
        torrents = []
        if not results:
            return torrents

        for result in results:
            # 解析分类信息
            raw_cat = result.get('category')
            cat_val = None

            category = MediaType.UNKNOWN.value

            if isinstance(raw_cat, dict):
                cat_val = raw_cat.get('slug') or raw_cat.get('name')
            elif isinstance(raw_cat, str):
                cat_val = raw_cat

            if cat_val:
                cat_val = str(cat_val).lower()
                if cat_val == self._movie_category:
                    category = MediaType.MOVIE.value
                elif cat_val == self._tv_category:
                    category = MediaType.TV.value
                elif cat_val == self._music_category:
                    category = MediaType.MUSIC.value
                else:
                    category = MediaType.UNKNOWN.value

            # 解析促销信息
            # API 后端已处理全站促销优先级，直接使用返回的 promotion 数据
            downloadvolumefactor = 1.0
            uploadvolumefactor = 1.0
            freedate = None

            promotion = result.get('promotion')
            if promotion and promotion.get('is_active'):
                downloadvolumefactor = float(promotion.get('down_multiplier', 1.0))
                uploadvolumefactor = float(promotion.get('up_multiplier', 1.0))
                # 促销到期时间，格式化为 YYYY-MM-DD HH:MM:SS
                if promotion.get('until'):
                    freedate = time_tools.normalize_datetime(promotion.get('until'))

            torrent = {
                'title': result.get('title'),
                'description': result.get('subtitle'),
                'enclosure': self.__get_download_url(result.get('id')),
                'pubdate': time_tools.normalize_datetime(result.get('created_at')),
                'size': int(result.get('size') or 0),
                'seeders': int(result.get('seeders') or 0),
                'peers': int(result.get('leechers') or 0),
                'grabs': int(result.get('downloads') or 0),
                'downloadvolumefactor': downloadvolumefactor,
                'uploadvolumefactor': uploadvolumefactor,
                'freedate': freedate,
                'page_url': f"https://{self._domain}/torrent/{result.get('uuid')}",
                'labels': [],
                'category': category
            }
            torrents.append(torrent)
        return torrents

    def search(self, keyword: str, mtype: MediaType = None, cat: Optional[str] = None, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        同步搜索种子

        :param keyword: 搜索关键词
        :param mtype: 媒体类型 (MOVIE/TV/MUSIC)
        :param cat: 用户选择的分类 ID（逗号分隔）
        :param page: 页码（从 0 开始）
        :return: (是否发生错误, 种子列表)
        """
        if not self._apikey:
            logger.warn(f"{self._name} 未配置 API Key (Passkey)")
            return True, []

        params = self.__get_params(keyword, mtype, cat, page)
        headers = {
            "Authorization": f"Bearer {self._apikey}",
            "Accept": "application/json"
        }

        res = RequestUtils(
            headers=headers,
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=self._searchurl, params=params)

        return self.__process_response(res)

    async def async_search(self, keyword: str, mtype: MediaType = None, cat: Optional[str] = None, page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        异步搜索种子

        :param keyword: 搜索关键词
        :param mtype: 媒体类型 (MOVIE/TV/MUSIC)
        :param cat: 用户选择的分类 ID（逗号分隔）
        :param page: 页码（从 0 开始）
        :return: (是否发生错误, 种子列表)
        """
        if not self._apikey:
            logger.warn(f"{self._name} 未配置 API Key (Passkey)")
            return True, []

        params = self.__get_params(keyword, mtype, cat, page)
        headers = {
            "Authorization": f"Bearer {self._apikey}",
            "Accept": "application/json"
        }

        res = await AsyncRequestUtils(
            headers=headers,
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=self._searchurl, params=params)

        return self.__process_response(res)

    def __get_download_url(self, torrent_id: int) -> str:
        """
        构建种子下载链接

        使用 base64 编码的方式告诉 MoviePilot 如何获取真实下载地址
        MoviePilot 会先请求详情接口，然后从响应中提取 data.download_url

        :param torrent_id: 种子 ID
        :return: base64 编码的请求配置字符串 + 详情接口 URL
        """
        url = self._downloadurl % torrent_id
        # MoviePilot 会解析这个特殊格式的 URL：
        # 1. 使用指定的 method 和 header 请求 URL
        # 2. 从 JSON 响应中提取 result 指定的字段值作为真实下载地址
        params = {
            'method': 'get',
            'header': {
                'Authorization': f'Bearer {self._apikey}',
                'Accept': 'application/json'
            },
            'result': 'data.download_url'
        }
        base64_str = base64.b64encode(json.dumps(params).encode('utf-8')).decode('utf-8')
        return f"[{base64_str}]{url}"
