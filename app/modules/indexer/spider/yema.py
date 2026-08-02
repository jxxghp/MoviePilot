import base64
import json
from typing import List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.string import StringUtils


class YemaSpider:
    """
    YemaPT 开放 API 索引器
    """

    _size = 100
    _movie_category = [4]
    _tv_category = [5, 6, 13, 14, 15, 16, 17]

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

    def __init__(self, indexer: dict):
        """
        初始化 YemaPT 开放 API 索引器

        :param indexer: 合并站点认证信息后的索引配置
        """
        indexer = indexer or {}
        self._name = indexer.get("name") or "YemaPT"
        self._site_url = str(indexer.get("domain") or "https://www.yemapt.org/").rstrip("/")
        self._proxy = settings.PROXY if indexer.get("proxy") else None
        self._use_proxy = bool(indexer.get("proxy"))
        self._user_agent = indexer.get("ua") or settings.USER_AGENT
        self._api_key = indexer.get("apikey")
        self._timeout = indexer.get("timeout") or 15
        self._search_url = f"{self._site_url}/openApi/torrent/fetchOpenTorrentList.json"
        self._download_key_url = f"{self._site_url}/openApi/torrent/generateDownloadKey.json"

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量

        :param keyword: 搜索关键字，YemaPT 不按关键字改变分页容量
        :return: 搜索接口单页容量
        """
        return cls._size

    def _request_headers(self) -> dict:
        """
        构造开放 API 请求头

        :return: 不包含 Cookie 的 AuthKey 请求头
        """
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self._user_agent,
        }

    def _build_params(
        self,
        keyword: Optional[str],
        page: Optional[int],
    ) -> dict:
        """
        构造公开种子列表查询参数

        :param keyword: 搜索关键字
        :param page: MoviePilot 从 0 开始的页码
        :return: YemaPT 开放 API 请求体
        """
        params = {
            "pageParam": {
                "current": int(page or 0) + 1,
                "pageSize": self._size,
            },
            "sorter": {},
        }
        if keyword:
            params["keyword"] = keyword
        return params

    def _parse_result(self, results: List[dict]) -> List[dict]:
        """
        将开放 API 种子数据转换为 MoviePilot 标准字段

        :param results: 公开种子列表接口 data 数组
        :return: MoviePilot 标准种子字典列表
        """
        torrents = []
        for result in results or []:
            if not isinstance(result, dict):
                continue
            category_value = result.get("categoryId")
            if category_value in self._tv_category:
                category = MediaType.TV.value
            elif category_value in self._movie_category:
                category = MediaType.MOVIE.value
            else:
                category = MediaType.UNKNOWN.value

            labels = [
                self._labels[label_id]
                for label_id in result.get("tagList") or []
                if label_id in self._labels
            ]
            torrent_id = result.get("id")
            torrents.append({
                "title": result.get("showName"),
                "description": result.get("shortDesc"),
                "enclosure": self._build_download_url(torrent_id),
                "pubdate": StringUtils.unify_datetime_str(result.get("listingTime")),
                "size": result.get("fileSize"),
                "seeders": result.get("seedNum"),
                "peers": result.get("leechNum"),
                "grabs": result.get("completedNum"),
                "downloadvolumefactor": self._download_factor(result.get("downloadPromotion")),
                "uploadvolumefactor": self._upload_factor(result.get("uploadPromotion")),
                "freedate": StringUtils.unify_datetime_str(result.get("downloadPromotionEndTime")),
                "page_url": f"{self._site_url}/#/torrent/detail/{torrent_id}/",
                "labels": labels,
                "hit_and_run": bool(result.get("hrPunishEnable")),
                "category": category,
            })
        return torrents

    def _process_search_response(self, response) -> Tuple[bool, List[dict]]:
        """
        校验开放 API 通用响应并解析搜索结果

        :param response: RequestUtils 返回的响应对象
        :return: 是否失败及标准种子列表
        """
        if response is None:
            logger.warning(f"{self._name} 搜索失败，无法连接开放 API")
            return True, []
        if response.status_code != 200:
            logger.warning(f"{self._name} 搜索失败，HTTP 错误码：{response.status_code}")
            return True, []
        try:
            payload = response.json() or {}
        except (TypeError, ValueError) as err:
            logger.warning(f"{self._name} 搜索响应不是有效 JSON：{str(err)}")
            return True, []
        if not isinstance(payload, dict):
            logger.warning(f"{self._name} 搜索响应结构无效")
            return True, []
        if not payload.get("success"):
            logger.warning(f"{self._name} 搜索失败：{payload.get('errorMessage') or '未知错误'}")
            return True, []
        results = payload.get("data")
        if not isinstance(results, list):
            logger.warning(f"{self._name} 搜索响应 data 不是数组")
            return True, []
        return False, self._parse_result(results)

    def search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        同步搜索 YemaPT 公开种子

        :param keyword: 搜索关键字
        :param mtype: MoviePilot 媒体类型，开放 API 不支持直接按媒体类型查询
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API AuthKey")
            return True, []
        response = RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(
            url=self._search_url,
            json=self._build_params(keyword, page),
        )
        return self._process_search_response(response)

    async def async_search(
        self,
        keyword: Optional[str],
        mtype: MediaType = None,
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索 YemaPT 公开种子

        :param keyword: 搜索关键字
        :param mtype: MoviePilot 媒体类型，开放 API 不支持直接按媒体类型查询
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_key:
            logger.warning(f"{self._name} 未配置 API AuthKey")
            return True, []
        response = await AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ).post_res(
            url=self._search_url,
            json=self._build_params(keyword, page),
        )
        return self._process_search_response(response)

    @staticmethod
    def _download_factor(promotion: str) -> float:
        """
        转换下载促销类型

        :param promotion: 开放 API 下载促销枚举
        :return: MoviePilot 下载系数
        """
        return {
            "free": 0,
            "half": 0.5,
            "none": 1,
        }.get(promotion, 1)

    @staticmethod
    def _upload_factor(promotion: str) -> float:
        """
        转换上传促销类型

        :param promotion: 开放 API 上传促销枚举
        :return: MoviePilot 上传系数
        """
        return {
            "none": 1,
            "one_half": 1.5,
            "double_upload": 2,
        }.get(promotion, 1)

    def _build_download_url(self, torrent_id: int) -> str:
        """
        构造先生成下载凭证再获取种子文件的两段式链接

        :param torrent_id: YemaPT 种子 ID
        :return: Base64 请求配置与下载凭证接口 URL
        """
        request_config = {
            "method": "post",
            "cookie": False,
            "header": {
                "Authorization": self._api_key,
                "Accept": "application/json",
            },
            "params": {"id": torrent_id},
            "proxy": self._use_proxy,
            "success": "success",
            "result": "data",
            "result_base_url": self._site_url,
            "result_path": "api/torrent/download1",
            "result_query_param": "token",
        }
        encoded_config = base64.b64encode(
            json.dumps(request_config).encode("utf-8")
        ).decode("ascii")
        return f"[{encoded_config}]{self._download_key_url}"
