"""
通过海胆接口搜索种子并转换为通用索引结果。
"""

import urllib.parse
from typing import Any, List, Optional, Tuple

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.configuration import get_configured_system_config
from app.domain import site as site_rules
from app.foundation import temporal as time_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


class HaiDanSpider:
    """
    通过海胆 API 搜索种子，按媒体分类转换索引结果。
    """
    _indexerid = None
    _domain = None
    _url = None
    _name: Optional[str] = ""
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
    # 音乐搜索同时覆盖高品质音频和音乐视频/演唱会分区。
    _music_category = ['406', '408']

    # 促销状态：1 普通、2 免费、3 双倍上传、4 双倍上传且免费、5 半价、6 双倍上传且半价、7 三折。
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

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> None:
        """
        海胆搜索入口当前没有接入页码参数，不参与自动翻页。

        :param keyword: 保留统一入口参数，当前不使用关键词判断分页
        :return: 始终返回 None
        """
        return None

    def __init__(self, indexer: dict[str, Any]):
        """
        使用站点配置初始化 HaiDan API 请求上下文。
        """
        self.systemconfig = get_configured_system_config()
        if indexer:
            self._indexerid = indexer.get('id')
            self._url = indexer.get('domain')
            self._domain = site_rules.extract_domain(self._url)
            self._searchurl = self._searchurl % self._url
            self._name = indexer.get('name')
            if indexer.get('proxy'):
                self._proxy = get_runtime_setting('PROXY')
            self._cookie = indexer.get('cookie')
            self._ua = indexer.get('ua')
            self._timeout = indexer.get('timeout') or 15

    def __get_params(self, keyword: str, mtype: Optional[MediaType] = None) -> str:
        """
        获取请求参数。
        """

        def __dict_to_query(_params: dict[str, object]) -> str:
            """
            将数组转换为逗号分隔的字符串。
            """
            for key, value in _params.items():
                if isinstance(value, list):
                    _params[key] = ','.join(map(str, value))
            return urllib.parse.urlencode(_params)

        if not mtype:
            categories = []
        elif mtype == MediaType.TV:
            categories = self._tv_category
        elif mtype == MediaType.MUSIC:
            categories = self._music_category
        else:
            categories = self._movie_category

        # 搜索类型
        if keyword and keyword.startswith('tt'):
            search_area = '4'
        else:
            search_area = '0'

        # urllib.urlencode 会把 None 编码成字面量 "None"，空关键词浏览时必须显式传空字符串。
        return __dict_to_query({
            "isapi": "1",
            # 0-标题 1-简介（较慢）3-发种用户名 4-IMDb
            "search_area": search_area,
            "search": keyword or "",
            # 0-与 1-或 2-精准
            "search_mode": "0",
            "cat": categories
        })

    def __parse_result(self, result: dict[str, Any]) -> list[dict[str, object]]:
        """
        解析结果。
        """
        torrents: list[dict[str, object]] = []
        data = result.get('data') or {}
        for tid, item in data.items():
            # 分类字段在每条种子内，接口顶层没有 category 字段
            category_value = str(item.get('category') or '')
            if category_value in self._music_category:
                category = MediaType.MUSIC.value
            elif category_value in self._tv_category \
                    and category_value not in self._movie_category:
                category = MediaType.TV.value
            elif category_value in self._movie_category:
                category = MediaType.MOVIE.value
            else:
                category = MediaType.UNKNOWN.value
            torrent: dict[str, object] = {
                'title': item.get('name'),
                'description': item.get('small_descr'),
                'enclosure': item.get('url'),
                'pubdate': time_tools.format_timestamp(item.get('added')),
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
        return torrents

    def __process_response(self, res: Any) -> Tuple[bool, List[dict[str, object]]]:
        """
        统一判定搜索响应状态、业务代码并投影 HaiDan 结果。
        """
        if res and res.status_code == 200:
            result = res.json()
            if result.get('code') != 0:
                logger.warn(f"{self._name} 搜索失败：{result.get('msg')}")
                return True, []
            return False, self.__parse_result(result)
        if res is not None:
            logger.warn(f"{self._name} 搜索失败，错误码：{res.status_code}")
            return True, []
        logger.warn(f"{self._name} 搜索失败，无法连接 {self._domain}")
        return True, []

    def search(self, keyword: str, mtype: Optional[MediaType] = None) -> Tuple[bool, List[dict[str, object]]]:
        """
        同步搜索 海胆 种子。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """

        if not self._cookie:
            return True, []

        params_str = self.__get_params(keyword, mtype)

        res = RequestUtils(
            cookies=self._cookie,
            ua=self._ua,
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=f"{self._searchurl}?{params_str}")
        return self.__process_response(res)

    async def async_search(self, keyword: str, mtype: Optional[MediaType] = None) -> Tuple[bool, List[dict[str, object]]]:
        """
        异步搜索 海胆 种子。

        :param keyword: 搜索关键词；为空时按站点接口规则浏览资源
        :param mtype: 目标媒体类型，未指定时不额外限制媒体类型
        :return: 是否发生错误及标准化资源列表；空列表也可能表示没有匹配资源
        """
        if not self._cookie:
            return True, []

        params_str = self.__get_params(keyword, mtype)

        res = await AsyncRequestUtils(
            cookies=self._cookie,
            ua=self._ua,
            proxies=self._proxy,
            timeout=self._timeout
        ).get_res(url=f"{self._searchurl}?{params_str}")

        return self.__process_response(res)

    def __get_downloadvolumefactor(self, discount: str) -> float:
        """
        获取下载系数。
        """
        if discount:
            return self._dl_state.get(discount, 1)
        return 1

    def __get_uploadvolumefactor(self, discount: str) -> float:
        """
        获取上传系数。
        """
        if discount:
            return self._up_state.get(discount, 1)
        return 1
