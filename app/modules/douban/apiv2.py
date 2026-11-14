# -*- coding: utf-8 -*-
"""
封装豆瓣影视与音乐查询、Frodo 接口签名及音乐官网页面解析。

保留同步和异步接口的响应及缓存约定。
"""

import base64
import hashlib
import hmac
import re
from datetime import datetime
from random import choice
from typing import Any, Callable, Optional, ParamSpec, Protocol, TypeVar, Union, cast
from urllib import parse

from bs4 import BeautifulSoup

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.music.observation import music_request_timeout, report_music_recognition
from app.foundation.singleton import WeakSingleton
from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting


def _empty_music_response(payload: object) -> bool:
    """
    音乐空搜索只短期缓存，不能沿用电影详情的长缓存周期。
    """
    return not payload or (isinstance(payload, dict) and any(
        key in payload and not payload[key] for key in ("items", "subjects")
    ))


_MusicParams = ParamSpec("_MusicParams")
_MusicReturn = TypeVar("_MusicReturn")


def _cached_music_request(function: Callable[_MusicParams, _MusicReturn]) -> Callable[_MusicParams, _MusicReturn]:
    """
    在保持原函数签名的缓存边界上应用音乐专用命名空间与短负缓存。
    """
    return cached(
        maxsize=get_runtime_setting('CONF').douban, ttl=get_runtime_setting('CONF').meta, skip_none=True,
        shared_key="music_get_v2", empty_ttl=300, empty_if=_empty_music_response,
    )(function)


_QueryValue = Optional[Union[str, int]]


class _JsonResponse(Protocol):
    """
    只读取同步及异步 HTTP 响应共有的 JSON 解码接口。
    """

    @property
    def status_code(self) -> int:
        """
        返回服务端 HTTP 状态码，用于区分音乐条目缺失和服务故障。

        :return: HTTP 状态码
        """
        ...

    def json(self) -> Any:
        """
        解码外部 JSON；嵌套字段由各豆瓣业务投影按对应接口解释。

        :return: HTTP 客户端解码得到的原始 JSON 内容
        :raises ValueError: 响应不是有效 JSON
        """
        ...


def _cached_api_request(
        shared_key: Optional[str] = None,
) -> Callable[[Callable[_MusicParams, _MusicReturn]], Callable[_MusicParams, _MusicReturn]]:
    """
    为豆瓣请求声明缓存装饰器保持参数和返回类型的契约。

    :param shared_key: 同步及异步接口共享的缓存名称；缺省时沿用函数名称
    :return: 保持原函数签名的缓存装饰器
    """
    def decorate(function: Callable[_MusicParams, _MusicReturn]) -> Callable[_MusicParams, _MusicReturn]:
        """
        保留底层装饰器的调用方式与缓存属性，不额外包装请求执行过程。
        """
        return cached(
            maxsize=get_runtime_setting('CONF').douban, ttl=get_runtime_setting('CONF').meta,
            skip_none=True, shared_key=shared_key,
        )(function)

    return decorate


class DoubanApi(metaclass=WeakSingleton):
    """
    封装豆瓣 Frodo API 与音乐官网公开浏览页面。
    """

    _urls = {
        # 搜索类
        # sort=U:近期热门 T:标记最多 S:评分最高 R:最新上映
        # 聚合搜索
        "search": "/search/weixin",
        "search_agg": "/search",
        "search_subject": "/search/subjects",
        "imdbid": "/movie/imdb/%s",

        # 电影探索
        # sort=U:综合排序 T:近期热度 S:高分优先 R:首播时间
        "movie_recommend": "/movie/recommend",
        # 电视剧探索
        "tv_recommend": "/tv/recommend",
        # 搜索
        "movie_tag": "/movie/tag",
        "tv_tag": "/tv/tag",
        "movie_search": "/search/movie",
        "tv_search": "/search/movie",
        "book_search": "/search/book",
        "group_search": "/search/group",

        # 各类主题合集
        # 正在上映
        "movie_showing": "/subject_collection/movie_showing/items",
        # 热门电影
        "movie_hot_gaia": "/subject_collection/movie_hot_gaia/items",
        # 即将上映
        "movie_soon": "/subject_collection/movie_soon/items",
        # 电影高分前 250 名。
        "movie_top250": "/subject_collection/movie_top250/items",
        # 高分经典科幻片榜
        "movie_scifi": "/subject_collection/movie_scifi/items",
        # 高分经典喜剧片榜
        "movie_comedy": "/subject_collection/movie_comedy/items",
        # 高分经典动作片榜
        "movie_action": "/subject_collection/movie_action/items",
        # 高分经典爱情片榜
        "movie_love": "/subject_collection/movie_love/items",

        # 热门剧集
        "tv_hot": "/subject_collection/tv_hot/items",
        # 国产剧
        "tv_domestic": "/subject_collection/tv_domestic/items",
        # 美剧
        "tv_american": "/subject_collection/tv_american/items",
        # 日剧。
        "tv_japanese": "/subject_collection/tv_japanese/items",
        # 韩剧
        "tv_korean": "/subject_collection/tv_korean/items",
        # 动画
        "tv_animation": "/subject_collection/tv_animation/items",
        # 综艺
        "tv_variety_show": "/subject_collection/tv_variety_show/items",
        # 华语口碑周榜
        "tv_chinese_best_weekly": "/subject_collection/tv_chinese_best_weekly/items",
        # 全球口碑周榜
        "tv_global_best_weekly": "/subject_collection/tv_global_best_weekly/items",

        # 热门综艺。
        "show_hot": "/subject_collection/show_hot/items",
        # 国内综艺
        "show_domestic": "/subject_collection/show_domestic/items",
        # 国外综艺
        "show_foreign": "/subject_collection/show_foreign/items",

        "book_bestseller": "/subject_collection/book_bestseller/items",
        "book_top250": "/subject_collection/book_top250/items",
        # 虚构类热门榜
        "book_fiction_hot_weekly": "/subject_collection/book_fiction_hot_weekly/items",
        # 非虚构类热门
        "book_nonfiction_hot_weekly": "/subject_collection/book_nonfiction_hot_weekly/items",

        # 音乐
        "music_single": "/subject_collection/music_single/items",

        # 分类排行榜。
        "movie_rank_list": "/movie/rank_list",
        "movie_year_ranks": "/movie/year_ranks",
        "book_rank_list": "/book/rank_list",
        "tv_rank_list": "/tv/rank_list",

        # 电影详情与辅助信息。
        "movie_detail": "/movie/",
        "movie_rating": "/movie/%s/rating",
        "movie_photos": "/movie/%s/photos",
        "movie_trailers": "/movie/%s/trailers",
        "movie_interests": "/movie/%s/interests",
        "movie_reviews": "/movie/%s/reviews",
        "movie_recommendations": "/movie/%s/recommendations",
        "movie_celebrities": "/movie/%s/celebrities",

        # 电视剧详情与辅助信息。
        "tv_detail": "/tv/",
        "tv_rating": "/tv/%s/rating",
        "tv_photos": "/tv/%s/photos",
        "tv_trailers": "/tv/%s/trailers",
        "tv_interests": "/tv/%s/interests",
        "tv_reviews": "/tv/%s/reviews",
        "tv_recommendations": "/tv/%s/recommendations",
        "tv_celebrities": "/tv/%s/celebrities",

        # 图书详情与辅助信息。
        "book_detail": "/book/",
        "book_rating": "/book/%s/rating",
        "book_interests": "/book/%s/interests",
        "book_reviews": "/book/%s/reviews",
        "book_recommendations": "/book/%s/recommendations",

        # 音乐详情与辅助信息。
        "music_detail": "/music/",
        "music_rating": "/music/%s/rating",
        "music_interests": "/music/%s/interests",
        "music_reviews": "/music/%s/reviews",
        "music_recommendations": "/music/%s/recommendations",

        # 豆列详情及条目。
        "doulist": "/doulist/",
        "doulist_items": "/doulist/%s/items",

        # 人物详情及作品集合。
        "person_detail": "/elessar/subject/",
        "person_work": "/elessar/work_collections/%s/works",
    }

    _user_agents = [
        "api-client/1 com.douban.frodo/7.22.0.beta9(231) Android/23 product/Mate 40 vendor/HUAWEI model/Mate 40 brand/HUAWEI  rom/android  network/wifi  platform/AndroidPad"
        "api-client/1 com.douban.frodo/7.18.0(230) Android/22 product/MI 9 vendor/Xiaomi model/MI 9 brand/Android  rom/miui6  network/wifi  platform/mobile nd/1",
        "api-client/1 com.douban.frodo/7.1.0(205) Android/29 product/perseus vendor/Xiaomi model/Mi MIX 3  rom/miui6  network/wifi  platform/mobile nd/1",
        "api-client/1 com.douban.frodo/7.3.0(207) Android/22 product/MI 9 vendor/Xiaomi model/MI 9 brand/Android  rom/miui6  network/wifi platform/mobile nd/1"]
    # Frodo 客户端签名协议常量，与接口所需应用标识配套使用。
    _api_secret_key = "bf7dddc7c9cfe6f7"
    _api_key = "0dad551ec0f84ed02907ff5c42e8ec70"
    _api_key2 = "0ab215a8b1977939201640fa14c66bab"
    _base_url = "https://frodo.douban.com/api/v2"
    _api_url = "https://api.douban.com/v2"
    _music_web_url = "https://music.douban.com"

    def __init__(self) -> None:
        """
        创建复用连接的同步 HTTP 客户端，由 close 释放连接。
        """
        self._request = RequestUtils(use_session=True)

    @classmethod
    def __sign(cls, url: str, ts: str, method: str = 'GET') -> str:
        """
        按 Frodo 接口规则对方法、路径和日期值生成 HMAC-SHA1 签名。

        :param url: 待签名完整 URL，仅使用其路径部分
        :param ts: 签名日期或时间值，需与请求 _ts 一致
        :param method: HTTP 方法名，签名前转为大写
        :return: Base64 编码的签名字符串
        """
        url_path = parse.urlparse(url).path
        raw_sign = '&'.join([method.upper(), parse.quote(url_path, safe=''), ts])
        return base64.b64encode(
            hmac.new(
                cls._api_secret_key.encode(),
                raw_sign.encode(),
                hashlib.sha1
            ).digest()
        ).decode()

    def __invoke_recommend(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        推荐/发现类API。
        """
        return self.__invoke(url, **kwargs)

    async def __async_invoke_recommend(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        推荐/发现类API（异步版本）。
        """
        return await self.__async_invoke(url, **kwargs)

    def __invoke_search(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        搜索类API。
        """
        return self.__invoke(url, **kwargs)

    async def __async_invoke_search(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        搜索类API（异步版本）。
        """
        return await self.__async_invoke(url, **kwargs)

    @staticmethod
    def _music_response_payload(response: Optional[_JsonResponse]) -> Optional[dict[str, Any]]:
        """
        将音乐服务故障与正常无条目分开，错误响应不能写成长时间负缓存。
        """
        if response is not None and response.status_code == 404:
            return {}
        if response is None or response.status_code != 200:
            report_music_recognition("service_error", "豆瓣音乐服务暂时不可用")
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            report_music_recognition("service_error", "豆瓣音乐返回了无效响应")
            return None
        if not isinstance(payload, dict) or payload.get("code") not in (None, 0, "0") or payload.get("error"):
            report_music_recognition("service_error", "豆瓣音乐返回了错误响应")
            return None
        return payload

    @_cached_music_request
    def _music_get(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        仅音乐查询消费当前识别预算；影视继续使用原有请求和缓存合同。
        """
        timeout = music_request_timeout(maximum=20)
        if timeout is None:
            return None
        req_url, params = self._prepare_get_request(url, **kwargs)
        response = self._request.get_res(url=req_url, params=params,
                                         headers={"User-Agent": choice(self._user_agents)}, timeout=timeout)
        try:
            return self._music_response_payload(response)
        finally:
            if response is not None:
                response.close()

    @_cached_music_request
    async def _async_music_get(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        音乐异步请求使用相同预算及缓存命名空间，响应始终释放。
        """
        timeout = music_request_timeout(maximum=20)
        if timeout is None:
            return None
        req_url, params = self._prepare_get_request(url, **kwargs)
        response = await AsyncRequestUtils(ua=choice(self._user_agents), timeout=20).get_res(url=req_url, params=params, timeout=timeout)
        try:
            return self._music_response_payload(response)
        finally:
            if response is not None:
                await response.aclose()

    def _prepare_get_request(self, url: str, **kwargs: _QueryValue) -> tuple[str, dict[str, _QueryValue]]:
        """
        准备GET请求的URL和参数。
        """
        req_url = self._base_url + url

        params: dict[str, _QueryValue] = {'apiKey': self._api_key}
        if kwargs:
            params.update(kwargs)

        ts = params.pop(
            '_ts',
            datetime.strftime(datetime.now(), '%Y%m%d')
        )
        params.update({
            'os_rom': 'android',
            'apiKey': self._api_key,
            '_ts': ts,
            # 所有公开入口的 _ts 均来自字符串参数，联合类型仅用于承载其他查询值。
            '_sig': self.__sign(url=req_url, ts=cast(str, ts))
        })
        return req_url, params

    @staticmethod
    def _handle_response(
        resp: Optional[_JsonResponse]
    ) -> Optional[dict[str, Any]]:
        """
        仅接受成功 HTTP 和业务响应，避免把错误对象保存为长期详情缓存。

        JSON 解码异常仍交由调用方处理；合法空列表响应保持原合同。
        """
        if resp is None or resp.status_code != 200:
            return None
        payload: dict[str, Any] = resp.json()
        if not isinstance(payload, (dict, list)):
            return None
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
            return None
        return payload

    @_cached_api_request(shared_key="get")
    def __invoke(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        GET请求。
        """
        req_url, params = self._prepare_get_request(url, **kwargs)
        resp = self._request.get_res(
            url=req_url,
            params=params,
            headers={"User-Agent": choice(self._user_agents)},
        )
        return self._handle_response(resp)

    @_cached_api_request(shared_key="get")
    async def __async_invoke(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        GET请求（异步版本）。
        """
        req_url, params = self._prepare_get_request(url, **kwargs)
        resp = await AsyncRequestUtils(
            ua=choice(self._user_agents)
        ).get_res(url=req_url, params=params)
        return self._handle_response(resp)

    def _prepare_post_request(self, url: str, **kwargs: _QueryValue) -> tuple[str, dict[str, _QueryValue]]:
        """
        准备POST请求的URL和参数。
        """
        req_url = self._api_url + url
        params: dict[str, _QueryValue] = {'apikey': self._api_key2}
        if kwargs:
            params.update(kwargs)
        if '_ts' in params:
            params.pop('_ts')
        return req_url, params

    @_cached_api_request(shared_key="post")
    def __post(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        POST请求。
        使用 `_api_key2` 以表单字段 `apikey` 提交，参数由 `_prepare_post_request` 组装
        """
        req_url, params = self._prepare_post_request(url, **kwargs)
        resp = self._request.post_res(
            url=req_url,
            data=params,
            headers={"User-Agent": get_runtime_setting('NORMAL_USER_AGENT')},
        )
        return self._handle_response(resp)

    @_cached_api_request(shared_key="post")
    async def __async_post(self, url: str, **kwargs: _QueryValue) -> Optional[dict[str, Any]]:
        """
        POST请求（异步版本）。
        """
        req_url, params = self._prepare_post_request(url, **kwargs)
        resp = await AsyncRequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT')
        ).post_res(url=req_url, data=params)
        return self._handle_response(resp)

    def imdbid(self, imdbid: str,
               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        IMDBID搜索。

        :param imdbid: IMDb 条目标识，如 tt 开头的影视编号
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__post(self._urls["imdbid"] % imdbid, _ts=ts)

    async def async_imdbid(self, imdbid: str,
                           ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        IMDBID搜索（异步版本）。

        :param imdbid: IMDb 条目标识，如 tt 开头的影视编号
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_post(self._urls["imdbid"] % imdbid, _ts=ts)

    def search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        关键字搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        关键字搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def movie_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_movie_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def tv_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_tv_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def book_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                    ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        书籍搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["book_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_book_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        书籍搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["book_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def music_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        搜索豆瓣音乐条目。

        消耗音乐识别预算；非空结果按配置缓存，正常空结果缓存 300 秒，服务错误不缓存。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 音乐响应字典；404 返回空字典，网络、业务或格式异常返回 None
        """
        return self._music_get(
            self._urls["search_subject"],
            type="music",
            q=keyword,
            start=start,
            count=count,
            _ts=ts,
        )

    async def async_music_search(self, keyword: str, start: Optional[int] = 0,
                                 count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        异步搜索豆瓣音乐条目。

        消耗音乐识别预算；非空结果按配置缓存，正常空结果缓存 300 秒，服务错误不缓存。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 音乐响应字典；404 返回空字典，网络、业务或格式异常返回 None
        """
        return await self._async_music_get(
            self._urls["search_subject"],
            type="music",
            q=keyword,
            start=start,
            count=count,
            _ts=ts,
        )

    def group_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        小组搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["group_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_group_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        小组搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["group_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def person_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        人物搜索。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["search_subject"], type="person", q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_person_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        人物搜索（异步版本）。

        :param keyword: 待搜索关键词
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["search_subject"], type="person", q=keyword,
                                                start=start, count=count, _ts=ts)

    def movie_showing(self, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        正在热映。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_showing"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_showing(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        正在热映（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_showing"],
                                                   start=start, count=count, _ts=ts)

    def movie_soon(self, start: Optional[int] = 0, count: Optional[int] = 20,
                   ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        即将上映。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_soon"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_soon(self, start: Optional[int] = 0, count: Optional[int] = 20,
                               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        即将上映（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_soon"],
                                                   start=start, count=count, _ts=ts)

    def movie_hot_gaia(self, start: Optional[int] = 0, count: Optional[int] = 20,
                       ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        热门电影。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_hot_gaia"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_hot_gaia(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                   ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        热门电影（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_hot_gaia"],
                                                   start=start, count=count, _ts=ts)

    def tv_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        热门剧集。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_hot"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        热门剧集（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_hot"],
                                                   start=start, count=count, _ts=ts)

    def tv_animation(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        动画。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_animation"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_animation(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        动画（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_animation"],
                                                   start=start, count=count, _ts=ts)

    def tv_variety_show(self, start: Optional[int] = 0, count: Optional[int] = 20,
                        ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        综艺。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_variety_show"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_variety_show(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                    ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        综艺（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_variety_show"],
                                                   start=start, count=count, _ts=ts)

    def tv_rank_list(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧排行榜。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_rank_list"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_rank_list(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧排行榜（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_rank_list"],
                                                   start=start, count=count, _ts=ts)

    def show_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        综艺热门。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["show_hot"],
                                       start=start, count=count, _ts=ts)

    async def async_show_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                             ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        综艺热门（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["show_hot"],
                                                   start=start, count=count, _ts=ts)

    def movie_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电影详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_detail"] + subject_id)

    async def async_movie_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电影详情（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_detail"] + subject_id)

    def movie_celebrities(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电影演职员。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_celebrities"] % subject_id)

    async def async_movie_celebrities(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电影演职员（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_celebrities"] % subject_id)

    def tv_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电视剧详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_detail"] + subject_id)

    async def async_tv_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电视剧详情（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_detail"] + subject_id)

    def tv_celebrities(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电视剧演职员。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_celebrities"] % subject_id)

    async def async_tv_celebrities(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        电视剧演职员（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_celebrities"] % subject_id)

    def book_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        书籍详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["book_detail"] + subject_id)

    async def async_book_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        书籍详情（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["book_detail"] + subject_id)

    def music_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        获取豆瓣音乐详情。

        消耗音乐识别预算；非空结果按配置缓存，正常空结果缓存 300 秒，服务错误不缓存。

        :param subject_id: 豆瓣条目原生标识
        :return: 音乐响应字典；404 返回空字典，网络、业务或格式异常返回 None
        """
        return self._music_get(self._urls["music_detail"] + subject_id)

    async def async_music_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        异步获取豆瓣音乐详情。

        消耗音乐识别预算；非空结果按配置缓存，正常空结果缓存 300 秒，服务错误不缓存。

        :param subject_id: 豆瓣条目原生标识
        :return: 音乐响应字典；404 返回空字典，网络、业务或格式异常返回 None
        """
        return await self._async_music_get(self._urls["music_detail"] + subject_id)

    def music_single(self, start: int = 0, count: int = 20) -> Optional[dict[str, Any]]:
        """
        分页获取豆瓣音乐推荐合集。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(
            self._urls["music_single"], start=start, count=count
        )

    async def async_music_single(self, start: int = 0, count: int = 20) -> Optional[dict[str, Any]]:
        """
        异步分页获取豆瓣音乐推荐合集。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(
            self._urls["music_single"], start=start, count=count
        )

    @_cached_api_request()
    def music_tag(
            self,
            tag: str,
            start: int = 0,
            count: int = 20,
            sort: str = "U",
    ) -> dict[str, Any]:
        """
        从豆瓣音乐官方标签页读取专辑，并适配为统一的音乐条目列表。

        官网每页 20 条，按所需偏移连续读取足够页；后续页失败时可能返回已收集的部分结果。

        :param tag: 单个音乐标签名称
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param sort: 豆瓣排序代码，按接口支持值传递；默认值依各入口而定
        :return: 含 items 列表的字典；无效标签或没有可解析条目时列表为空
        """
        normalized_tag = str(tag or "").strip()
        if not normalized_tag:
            return {"items": []}
        page_size = 20
        first_page = max(start, 0) // page_size
        first_offset = max(start, 0) % page_size
        required = first_offset + max(count, 1)
        items: list[dict[str, Any]] = []
        page = first_page
        while len(items) < required:
            url = f"{self._music_web_url}/tag/{parse.quote(normalized_tag, safe='')}"
            response = RequestUtils(
                ua=get_runtime_setting('NORMAL_USER_AGENT'),
                proxies=get_runtime_setting('PROXY'),
                timeout=20,
                accept_type="text/html,application/xhtml+xml",
            ).get_res(url=url, params={"start": page * page_size, "type": sort})
            page_items = self._parse_music_tag_page(response.content if response else b"")
            if not page_items:
                break
            items.extend(page_items)
            if len(page_items) < page_size:
                break
            page += 1
        return {"items": items[first_offset:first_offset + max(count, 1)]}

    @_cached_api_request()
    def music_chart(self) -> dict[str, Any]:
        """
        从豆瓣音乐官方榜单页读取新碟榜，并补充专辑详情供卡片展示。

        逐个补充专辑详情；详情不可用时保留榜单摘要。

        :return: 含新碟榜 items 的字典；无榜单条目时为空列表
        """
        response = RequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=20,
            accept_type="text/html,application/xhtml+xml",
        ).get_res(url=f"{self._music_web_url}/chart")
        chart_items = self._parse_music_chart_page(response.content if response else b"")
        items: list[dict[str, Any]] = []
        for chart_item in chart_items:
            detail = self.music_detail(subject_id=chart_item["id"])
            if isinstance(detail, dict) and detail:
                detail.setdefault("id", chart_item["id"])
                detail.setdefault("title", chart_item["title"])
                if chart_item.get("artists") and not detail.get("artists"):
                    detail["artists"] = chart_item["artists"]
                items.append(detail)
            else:
                items.append(chart_item)
        return {"items": items}

    @staticmethod
    def _parse_music_tag_page(content: bytes) -> list[dict[str, Any]]:
        """
        解析豆瓣音乐标签页中的专辑 ID、封面、发行信息和评分。
        """
        if not content:
            return []
        soup = BeautifulSoup(content, "html.parser")
        items: list[dict[str, Any]] = []
        for row in soup.select("tr.item[id]"):
            media_id = str(row.get("id") or "").strip()
            title_link = row.select_one("div.pl2 > a[href*='/subject/']")
            if not media_id or not title_link:
                continue
            title = next(title_link.stripped_strings, "").strip()
            metadata = row.select_one("div.pl2 > p.pl")
            metadata_text = metadata.get_text(" ", strip=True) if metadata else ""
            parts = [part.strip() for part in metadata_text.split("/") if part.strip()]
            release_date = next(
                (part for part in parts if re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", part)),
                None,
            )
            image = row.select_one("a.nbg img")
            rating_node = row.select_one("span.rating_nums")
            votes_node = row.select_one("div.star span.pl")
            votes_match = re.search(r"(\d+)\s*人评价", votes_node.get_text(" ", strip=True) if votes_node else "")
            item = {
                "id": media_id,
                "type": "music",
                "title": title,
                "artists": [{"name": parts[0]}] if parts else [],
                "pubdate": [release_date] if release_date else [],
                "year": release_date[:4] if release_date else None,
                "cover_url": image.get("src") if image else None,
                "rating": {
                    "value": rating_node.get_text(strip=True) if rating_node else None,
                    "count": votes_match.group(1) if votes_match else None,
                },
            }
            items.append(item)
        return items

    @staticmethod
    def _parse_music_chart_page(content: bytes) -> list[dict[str, Any]]:
        """
        解析豆瓣新碟榜的专辑 ID、标题和艺术家。
        """
        if not content:
            return []
        soup = BeautifulSoup(content, "html.parser")
        heading = next(
            (node for node in soup.select("h2") if "豆瓣新碟榜" in node.get_text()),
            None,
        )
        container = heading.parent if heading else None
        items: list[dict[str, Any]] = []
        for row in container.select("ul.col3 li") if container else []:
            link = row.select_one("p.entry a[href*='/subject/']")
            if not link:
                continue
            match = re.search(r"/subject/(\d+)", str(link.get("href") or ""))
            if not match:
                continue
            entry_text = row.select_one("p.entry").get_text(" ", strip=True)
            artist = entry_text.split("/", 1)[1].strip() if "/" in entry_text else ""
            items.append({
                "id": match.group(1),
                "type": "music",
                "title": link.get_text(" ", strip=True),
                "artists": [{"name": artist}] if artist else [],
            })
        return items

    def music_recommendations(
            self,
            subject_id: str,
            start: int = 0,
            count: int = 20,
    ) -> Optional[dict[str, Any]]:
        """
        获取豆瓣音乐条目的相关推荐。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(
            self._urls["music_recommendations"] % subject_id,
            start=start,
            count=count,
        )

    async def async_music_recommendations(
            self,
            subject_id: str,
            start: int = 0,
            count: int = 20,
    ) -> Optional[dict[str, Any]]:
        """
        异步获取豆瓣音乐条目的相关推荐。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(
            self._urls["music_recommendations"] % subject_id,
            start=start,
            count=count,
        )

    def movie_top250(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影TOP250。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_top250"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_top250(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影TOP250（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_top250"],
                                                   start=start, count=count, _ts=ts)

    def movie_recommend(self, tags: str = '', sort: str = 'R', start: Optional[int] = 0, count: Optional[int] = 20,
                        ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影探索。

        :param tags: 标签筛选字符串，分隔语义由对应豆瓣接口处理
        :param sort: 豆瓣排序代码，按接口支持值传递；默认值依各入口而定
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_recommend"], tags=tags, sort=sort,
                                       start=start, count=count, _ts=ts)

    async def async_movie_recommend(self, tags: str = '', sort: str = 'R', start: Optional[int] = 0, count: Optional[int] = 20,
                                    ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影探索（异步版本）。

        :param tags: 标签筛选字符串，分隔语义由对应豆瓣接口处理
        :param sort: 豆瓣排序代码，按接口支持值传递；默认值依各入口而定
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_recommend"], tags=tags, sort=sort,
                                                   start=start, count=count, _ts=ts)

    def tv_recommend(self, tags: str = '', sort: str = 'R', start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧探索。

        :param tags: 标签筛选字符串，分隔语义由对应豆瓣接口处理
        :param sort: 豆瓣排序代码，按接口支持值传递；默认值依各入口而定
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_recommend"], tags=tags, sort=sort,
                                       start=start, count=count, _ts=ts)

    async def async_tv_recommend(self, tags: str = '', sort: str = 'R', start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧探索（异步版本）。

        :param tags: 标签筛选字符串，分隔语义由对应豆瓣接口处理
        :param sort: 豆瓣排序代码，按接口支持值传递；默认值依各入口而定
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_recommend"], tags=tags, sort=sort,
                                                   start=start, count=count, _ts=ts)

    def tv_chinese_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        华语口碑周榜。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_chinese_best_weekly"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_chinese_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                           ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        华语口碑周榜（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_chinese_best_weekly"],
                                                   start=start, count=count, _ts=ts)

    def tv_global_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        全球口碑周榜。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_global_best_weekly"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_global_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                          ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        全球口碑周榜（异步版本）。

        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_global_best_weekly"],
                                                   start=start, count=count, _ts=ts)

    def doulist_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        豆列详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["doulist"] + subject_id)

    async def async_doulist_detail(self, subject_id: str) -> Optional[dict[str, Any]]:
        """
        豆列详情（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["doulist"] + subject_id)

    def doulist_items(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        豆列列表。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["doulist_items"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_doulist_items(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        豆列列表（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["doulist_items"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def movie_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[Union[dict[str, Any], list[dict[str, Any]]]]:
        """
        电影推荐，保留原生列表或携带 items 的字典响应。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["movie_recommendations"] % subject_id,
                                       start=start, count=count, _ts=ts)

    async def async_movie_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                          ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[Union[dict[str, Any], list[dict[str, Any]]]]:
        """
        电影推荐（异步版本），保留原生列表或携带 items 的字典响应。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["movie_recommendations"] % subject_id,
                                                   start=start, count=count, _ts=ts)

    def tv_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[Union[dict[str, Any], list[dict[str, Any]]]]:
        """
        电视剧推荐，保留原生列表或携带 items 的字典响应。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_recommend(self._urls["tv_recommendations"] % subject_id,
                                       start=start, count=count, _ts=ts)

    async def async_tv_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                       ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[Union[dict[str, Any], list[dict[str, Any]]]]:
        """
        电视剧推荐（异步版本），保留原生列表或携带 items 的字典响应。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_recommend(self._urls["tv_recommendations"] % subject_id,
                                                   start=start, count=count, _ts=ts)

    def movie_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影剧照。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_photos"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_movie_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影剧照（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_photos"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def tv_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧剧照。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_photos"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_tv_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧剧照（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_photos"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def movie_interests(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                        ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影短评。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_interests"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_movie_interests(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                    ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影短评（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_interests"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def tv_interests(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧短评。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_interests"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_tv_interests(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧短评（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_interests"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def movie_reviews(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影影评。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["movie_reviews"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_movie_reviews(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电影影评（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["movie_reviews"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def tv_reviews(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                   ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧影评。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["tv_reviews"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_tv_reviews(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                               ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        电视剧影评（异步版本）。

        :param subject_id: 豆瓣条目原生标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["tv_reviews"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def person_detail(self, subject_id: int) -> Optional[dict[str, Any]]:
        """
        查询豆瓣人物详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["person_detail"] + str(subject_id))

    async def async_person_detail(self, subject_id: int) -> Optional[dict[str, Any]]:
        """
        查询豆瓣人物详情。

        :param subject_id: 豆瓣条目原生标识
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["person_detail"] + str(subject_id))

    def person_work(self, subject_id: int, start: Optional[int] = 0, count: Optional[int] = 20,
                    sort_by: Optional[str] = "time",
                    collection_title: Optional[str] = "影视",
                    ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        查询豆瓣人物作品集合。

        :param subject_id: 人物详情中 work_collections 对应的作品集合标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param sort_by: 作品排序方式，collection、time 或 vote
        :param collection_title: 作品集合类型，如影视、图书或音乐
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return self.__invoke_search(self._urls["person_work"] % subject_id, sortby=sort_by,
                                    collection_title=collection_title,
                                    start=start, count=count, _ts=ts)

    async def async_person_work(self, subject_id: int, start: Optional[int] = 0, count: Optional[int] = 20,
                                sort_by: Optional[str] = "time",
                                collection_title: Optional[str] = "影视",
                                ts: str = datetime.strftime(datetime.now(), '%Y%m%d')) -> Optional[dict[str, Any]]:
        """
        查询豆瓣人物作品集合。

        :param subject_id: 人物详情中 work_collections 对应的作品集合标识
        :param start: 从 0 开始的条目偏移量，不是页码
        :param count: 本次最多请求的条目数
        :param sort_by: 作品排序方式，collection、time 或 vote
        :param collection_title: 作品集合类型，如影视、图书或音乐
        :param ts: YYYYMMDD 格式的签名与缓存区分值；默认值在模块加载时确定
        :return: 接口原始 JSON 响应；未取得 HTTP 响应时返回 None
        """
        return await self.__async_invoke_search(self._urls["person_work"] % subject_id, sortby=sort_by,
                                                collection_title=collection_title,
                                                start=start, count=count, _ts=ts)

    def clear_cache(self) -> None:
        """
        清空LRU缓存。
        """
        getattr(self.__invoke, "cache_clear")()
        getattr(self._music_get, "cache_clear")()
        getattr(self.__post, "cache_clear")()

    def close(self) -> None:
        """
        关闭豆瓣同步 HTTP 客户端。
        """
        self._request.close()
