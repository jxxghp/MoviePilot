# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import re
from datetime import datetime
from random import choice
from typing import Optional, Union
from urllib import parse

import httpx2
import requests
from bs4 import BeautifulSoup

from app.runtime.cache import cached
from app.runtime.config import settings
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.foundation.singleton import WeakSingleton


class DoubanApi(metaclass=WeakSingleton):
    """封装豆瓣 Frodo API 与音乐官网公开浏览页面。"""

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
        # TOP250
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
        # 本剧
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

        # 执门综艺
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

        # rank list
        "movie_rank_list": "/movie/rank_list",
        "movie_year_ranks": "/movie/year_ranks",
        "book_rank_list": "/book/rank_list",
        "tv_rank_list": "/tv/rank_list",

        # movie info
        "movie_detail": "/movie/",
        "movie_rating": "/movie/%s/rating",
        "movie_photos": "/movie/%s/photos",
        "movie_trailers": "/movie/%s/trailers",
        "movie_interests": "/movie/%s/interests",
        "movie_reviews": "/movie/%s/reviews",
        "movie_recommendations": "/movie/%s/recommendations",
        "movie_celebrities": "/movie/%s/celebrities",

        # tv info
        "tv_detail": "/tv/",
        "tv_rating": "/tv/%s/rating",
        "tv_photos": "/tv/%s/photos",
        "tv_trailers": "/tv/%s/trailers",
        "tv_interests": "/tv/%s/interests",
        "tv_reviews": "/tv/%s/reviews",
        "tv_recommendations": "/tv/%s/recommendations",
        "tv_celebrities": "/tv/%s/celebrities",

        # book info
        "book_detail": "/book/",
        "book_rating": "/book/%s/rating",
        "book_interests": "/book/%s/interests",
        "book_reviews": "/book/%s/reviews",
        "book_recommendations": "/book/%s/recommendations",

        # music info
        "music_detail": "/music/",
        "music_rating": "/music/%s/rating",
        "music_interests": "/music/%s/interests",
        "music_reviews": "/music/%s/reviews",
        "music_recommendations": "/music/%s/recommendations",

        # doulist
        "doulist": "/doulist/",
        "doulist_items": "/doulist/%s/items",

        # person
        "person_detail": "/elessar/subject/",
        "person_work": "/elessar/work_collections/%s/works",
    }

    _user_agents = [
        "api-client/1 com.douban.frodo/7.22.0.beta9(231) Android/23 product/Mate 40 vendor/HUAWEI model/Mate 40 brand/HUAWEI  rom/android  network/wifi  platform/AndroidPad"
        "api-client/1 com.douban.frodo/7.18.0(230) Android/22 product/MI 9 vendor/Xiaomi model/MI 9 brand/Android  rom/miui6  network/wifi  platform/mobile nd/1",
        "api-client/1 com.douban.frodo/7.1.0(205) Android/29 product/perseus vendor/Xiaomi model/Mi MIX 3  rom/miui6  network/wifi  platform/mobile nd/1",
        "api-client/1 com.douban.frodo/7.3.0(207) Android/22 product/MI 9 vendor/Xiaomi model/MI 9 brand/Android  rom/miui6  network/wifi platform/mobile nd/1"]
    _api_secret_key = "bf7dddc7c9cfe6f7"
    _api_key = "0dad551ec0f84ed02907ff5c42e8ec70"
    _api_key2 = "0ab215a8b1977939201640fa14c66bab"
    _base_url = "https://frodo.douban.com/api/v2"
    _api_url = "https://api.douban.com/v2"
    _music_web_url = "https://music.douban.com"

    def __init__(self):
        self._session = requests.Session()

    @classmethod
    def __sign(cls, url: str, ts: str, method='GET') -> str:
        """
        签名
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

    def __invoke_recommend(self, url: str, **kwargs) -> dict:
        """
        推荐/发现类API
        """
        return self.__invoke(url, **kwargs)

    async def __async_invoke_recommend(self, url: str, **kwargs) -> dict:
        """
        推荐/发现类API（异步版本）
        """
        return await self.__async_invoke(url, **kwargs)

    def __invoke_search(self, url: str, **kwargs) -> dict:
        """
        搜索类API
        """
        return self.__invoke(url, **kwargs)

    async def __async_invoke_search(self, url: str, **kwargs) -> dict:
        """
        搜索类API（异步版本）
        """
        return await self.__async_invoke(url, **kwargs)

    def _prepare_get_request(self, url: str, **kwargs) -> tuple[str, dict]:
        """
        准备GET请求的URL和参数
        """
        req_url = self._base_url + url

        params: dict = {'apiKey': self._api_key}
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
            '_sig': self.__sign(url=req_url, ts=ts)
        })
        return req_url, params

    @staticmethod
    def _handle_response(
        resp: Union[requests.Response, httpx2.Response]
    ) -> dict:
        """
        处理HTTP响应
        """
        return resp.json() if resp is not None else None

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True, shared_key="get")
    def __invoke(self, url: str, **kwargs) -> dict:
        """
        GET请求
        """
        req_url, params = self._prepare_get_request(url, **kwargs)
        resp = RequestUtils(
            ua=choice(self._user_agents),
            session=self._session
        ).get_res(url=req_url, params=params)
        return self._handle_response(resp)

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True, shared_key="get")
    async def __async_invoke(self, url: str, **kwargs) -> dict:
        """
        GET请求（异步版本）
        """
        req_url, params = self._prepare_get_request(url, **kwargs)
        resp = await AsyncRequestUtils(
            ua=choice(self._user_agents)
        ).get_res(url=req_url, params=params)
        return self._handle_response(resp)

    def _prepare_post_request(self, url: str, **kwargs) -> tuple[str, dict]:
        """
        准备POST请求的URL和参数
        """
        req_url = self._api_url + url
        params = {'apikey': self._api_key2}
        if kwargs:
            params.update(kwargs)
        if '_ts' in params:
            params.pop('_ts')
        return req_url, params

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True, shared_key="post")
    def __post(self, url: str, **kwargs) -> dict:
        """
        POST请求
        esponse = requests.post(
            url="https://api.douban.com/v2/movie/imdb/tt29139455",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Cookie": "bid=J9zb1zA5sJc",
            },
            data={
                "apikey": "0ab215a8b1977939201640fa14c66bab",
            }
        )
        """
        req_url, params = self._prepare_post_request(url, **kwargs)
        resp = RequestUtils(
            ua=settings.NORMAL_USER_AGENT,
            session=self._session,
        ).post_res(url=req_url, data=params)
        return self._handle_response(resp)

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True, shared_key="post")
    async def __async_post(self, url: str, **kwargs) -> dict:
        """
        POST请求（异步版本）
        """
        req_url, params = self._prepare_post_request(url, **kwargs)
        resp = await AsyncRequestUtils(
            ua=settings.NORMAL_USER_AGENT
        ).post_res(url=req_url, data=params)
        return self._handle_response(resp)

    def imdbid(self, imdbid: str,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        IMDBID搜索
        """
        return self.__post(self._urls["imdbid"] % imdbid, _ts=ts)

    async def async_imdbid(self, imdbid: str,
                           ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        IMDBID搜索（异步版本）
        """
        return await self.__async_post(self._urls["imdbid"] % imdbid, _ts=ts)

    def search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')) -> dict:
        """
        关键字搜索
        """
        return self.__invoke_search(self._urls["search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts=datetime.strftime(datetime.now(), '%Y%m%d')) -> dict:
        """
        关键字搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def movie_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影搜索
        """
        return self.__invoke_search(self._urls["movie_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_movie_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["movie_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def tv_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视搜索
        """
        return self.__invoke_search(self._urls["tv_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_tv_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["tv_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def book_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                    ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        书籍搜索
        """
        return self.__invoke_search(self._urls["book_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_book_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        书籍搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["book_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def music_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')) -> dict:
        """搜索豆瓣音乐条目。"""
        return self.__invoke_search(
            self._urls["search_subject"],
            type="music",
            q=keyword,
            start=start,
            count=count,
            _ts=ts,
        )

    async def async_music_search(self, keyword: str, start: Optional[int] = 0,
                                 count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')) -> dict:
        """异步搜索豆瓣音乐条目。"""
        return await self.__async_invoke_search(
            self._urls["search_subject"],
            type="music",
            q=keyword,
            start=start,
            count=count,
            _ts=ts,
        )

    def group_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        小组搜索
        """
        return self.__invoke_search(self._urls["group_search"], q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_group_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        小组搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["group_search"], q=keyword,
                                                start=start, count=count, _ts=ts)

    def person_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        人物搜索
        """
        return self.__invoke_search(self._urls["search_subject"], type="person", q=keyword,
                                    start=start, count=count, _ts=ts)

    async def async_person_search(self, keyword: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        人物搜索（异步版本）
        """
        return await self.__async_invoke_search(self._urls["search_subject"], type="person", q=keyword,
                                                start=start, count=count, _ts=ts)

    def movie_showing(self, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        正在热映
        """
        return self.__invoke_recommend(self._urls["movie_showing"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_showing(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        正在热映（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["movie_showing"],
                                                   start=start, count=count, _ts=ts)

    def movie_soon(self, start: Optional[int] = 0, count: Optional[int] = 20,
                   ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        即将上映
        """
        return self.__invoke_recommend(self._urls["movie_soon"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_soon(self, start: Optional[int] = 0, count: Optional[int] = 20,
                               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        即将上映（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["movie_soon"],
                                                   start=start, count=count, _ts=ts)

    def movie_hot_gaia(self, start: Optional[int] = 0, count: Optional[int] = 20,
                       ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门电影
        """
        return self.__invoke_recommend(self._urls["movie_hot_gaia"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_hot_gaia(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                   ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门电影（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["movie_hot_gaia"],
                                                   start=start, count=count, _ts=ts)

    def tv_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门剧集
        """
        return self.__invoke_recommend(self._urls["tv_hot"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门剧集（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_hot"],
                                                   start=start, count=count, _ts=ts)

    def tv_animation(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        动画
        """
        return self.__invoke_recommend(self._urls["tv_animation"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_animation(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        动画（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_animation"],
                                                   start=start, count=count, _ts=ts)

    def tv_variety_show(self, start: Optional[int] = 0, count: Optional[int] = 20,
                        ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺
        """
        return self.__invoke_recommend(self._urls["tv_variety_show"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_variety_show(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                    ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_variety_show"],
                                                   start=start, count=count, _ts=ts)

    def tv_rank_list(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧排行榜
        """
        return self.__invoke_recommend(self._urls["tv_rank_list"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_rank_list(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧排行榜（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_rank_list"],
                                                   start=start, count=count, _ts=ts)

    def show_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺热门
        """
        return self.__invoke_recommend(self._urls["show_hot"],
                                       start=start, count=count, _ts=ts)

    async def async_show_hot(self, start: Optional[int] = 0, count: Optional[int] = 20,
                             ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺热门（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["show_hot"],
                                                   start=start, count=count, _ts=ts)

    def movie_detail(self, subject_id: str):
        """
        电影详情
        """
        return self.__invoke_search(self._urls["movie_detail"] + subject_id)

    async def async_movie_detail(self, subject_id: str):
        """
        电影详情（异步版本）
        """
        return await self.__async_invoke_search(self._urls["movie_detail"] + subject_id)

    def movie_celebrities(self, subject_id: str):
        """
        电影演职员
        """
        return self.__invoke_search(self._urls["movie_celebrities"] % subject_id)

    async def async_movie_celebrities(self, subject_id: str):
        """
        电影演职员（异步版本）
        """
        return await self.__async_invoke_search(self._urls["movie_celebrities"] % subject_id)

    def tv_detail(self, subject_id: str):
        """
        电视剧详情
        """
        return self.__invoke_search(self._urls["tv_detail"] + subject_id)

    async def async_tv_detail(self, subject_id: str):
        """
        电视剧详情（异步版本）
        """
        return await self.__async_invoke_search(self._urls["tv_detail"] + subject_id)

    def tv_celebrities(self, subject_id: str):
        """
        电视剧演职员
        """
        return self.__invoke_search(self._urls["tv_celebrities"] % subject_id)

    async def async_tv_celebrities(self, subject_id: str):
        """
        电视剧演职员（异步版本）
        """
        return await self.__async_invoke_search(self._urls["tv_celebrities"] % subject_id)

    def book_detail(self, subject_id: str):
        """
        书籍详情
        """
        return self.__invoke_search(self._urls["book_detail"] + subject_id)

    async def async_book_detail(self, subject_id: str):
        """
        书籍详情（异步版本）
        """
        return await self.__async_invoke_search(self._urls["book_detail"] + subject_id)

    def music_detail(self, subject_id: str) -> dict:
        """获取豆瓣音乐详情。"""
        return self.__invoke_search(self._urls["music_detail"] + subject_id)

    async def async_music_detail(self, subject_id: str) -> dict:
        """异步获取豆瓣音乐详情。"""
        return await self.__async_invoke_search(self._urls["music_detail"] + subject_id)

    def music_single(self, start: int = 0, count: int = 20) -> dict:
        """分页获取豆瓣音乐推荐合集。"""
        return self.__invoke_recommend(
            self._urls["music_single"], start=start, count=count
        )

    async def async_music_single(self, start: int = 0, count: int = 20) -> dict:
        """异步分页获取豆瓣音乐推荐合集。"""
        return await self.__async_invoke_recommend(
            self._urls["music_single"], start=start, count=count
        )

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True)
    def music_tag(
            self,
            tag: str,
            start: int = 0,
            count: int = 20,
            sort: str = "U",
    ) -> dict:
        """从豆瓣音乐官方标签页读取专辑，并适配为统一的音乐条目列表。"""
        normalized_tag = str(tag or "").strip()
        if not normalized_tag:
            return {"items": []}
        page_size = 20
        first_page = max(start, 0) // page_size
        first_offset = max(start, 0) % page_size
        required = first_offset + max(count, 1)
        items = []
        page = first_page
        while len(items) < required:
            url = f"{self._music_web_url}/tag/{parse.quote(normalized_tag, safe='')}"
            response = RequestUtils(
                ua=settings.NORMAL_USER_AGENT,
                proxies=settings.PROXY,
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

    @cached(maxsize=settings.CONF.douban, ttl=settings.CONF.meta, skip_none=True)
    def music_chart(self) -> dict:
        """从豆瓣音乐官方榜单页读取新碟榜，并补充专辑详情供卡片展示。"""
        response = RequestUtils(
            ua=settings.NORMAL_USER_AGENT,
            proxies=settings.PROXY,
            timeout=20,
            accept_type="text/html,application/xhtml+xml",
        ).get_res(url=f"{self._music_web_url}/chart")
        chart_items = self._parse_music_chart_page(response.content if response else b"")
        items = []
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
    def _parse_music_tag_page(content: bytes) -> list[dict]:
        """解析豆瓣音乐标签页中的专辑 ID、封面、发行信息和评分。"""
        if not content:
            return []
        soup = BeautifulSoup(content, "html.parser")
        items = []
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
    def _parse_music_chart_page(content: bytes) -> list[dict]:
        """解析豆瓣新碟榜的专辑 ID、标题和艺术家。"""
        if not content:
            return []
        soup = BeautifulSoup(content, "html.parser")
        heading = next(
            (node for node in soup.select("h2") if "豆瓣新碟榜" in node.get_text()),
            None,
        )
        container = heading.parent if heading else None
        items = []
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
    ) -> dict:
        """获取豆瓣音乐条目的相关推荐。"""
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
    ) -> dict:
        """异步获取豆瓣音乐条目的相关推荐。"""
        return await self.__async_invoke_recommend(
            self._urls["music_recommendations"] % subject_id,
            start=start,
            count=count,
        )

    def movie_top250(self, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影TOP250
        """
        return self.__invoke_recommend(self._urls["movie_top250"],
                                       start=start, count=count, _ts=ts)

    async def async_movie_top250(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影TOP250（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["movie_top250"],
                                                   start=start, count=count, _ts=ts)

    def movie_recommend(self, tags='', sort='R', start: Optional[int] = 0, count: Optional[int] = 20,
                        ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影探索
        """
        return self.__invoke_recommend(self._urls["movie_recommend"], tags=tags, sort=sort,
                                       start=start, count=count, _ts=ts)

    async def async_movie_recommend(self, tags='', sort='R', start: Optional[int] = 0, count: Optional[int] = 20,
                                    ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影探索（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["movie_recommend"], tags=tags, sort=sort,
                                                   start=start, count=count, _ts=ts)

    def tv_recommend(self, tags='', sort='R', start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧探索
        """
        return self.__invoke_recommend(self._urls["tv_recommend"], tags=tags, sort=sort,
                                       start=start, count=count, _ts=ts)

    async def async_tv_recommend(self, tags='', sort='R', start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧探索（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_recommend"], tags=tags, sort=sort,
                                                   start=start, count=count, _ts=ts)

    def tv_chinese_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        华语口碑周榜
        """
        return self.__invoke_recommend(self._urls["tv_chinese_best_weekly"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_chinese_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                           ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        华语口碑周榜（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_chinese_best_weekly"],
                                                   start=start, count=count, _ts=ts)

    def tv_global_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        全球口碑周榜
        """
        return self.__invoke_recommend(self._urls["tv_global_best_weekly"],
                                       start=start, count=count, _ts=ts)

    async def async_tv_global_best_weekly(self, start: Optional[int] = 0, count: Optional[int] = 20,
                                          ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        全球口碑周榜（异步版本）
        """
        return await self.__async_invoke_recommend(self._urls["tv_global_best_weekly"],
                                                   start=start, count=count, _ts=ts)

    def doulist_detail(self, subject_id: str):
        """
        豆列详情
        :param subject_id: 豆列id
        """
        return self.__invoke_search(self._urls["doulist"] + subject_id)

    async def async_doulist_detail(self, subject_id: str):
        """
        豆列详情（异步版本）
        :param subject_id: 豆列id
        """
        return await self.__async_invoke_search(self._urls["doulist"] + subject_id)

    def doulist_items(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        豆列列表
        :param subject_id: 豆列id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke_search(self._urls["doulist_items"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_doulist_items(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        豆列列表（异步版本）
        :param subject_id: 豆列id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return await self.__async_invoke_search(self._urls["doulist_items"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def movie_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影推荐
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke_recommend(self._urls["movie_recommendations"] % subject_id,
                                       start=start, count=count, _ts=ts)

    async def async_movie_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                          ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影推荐（异步版本）
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return await self.__async_invoke_recommend(self._urls["movie_recommendations"] % subject_id,
                                                   start=start, count=count, _ts=ts)

    def tv_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                           ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧推荐
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke_recommend(self._urls["tv_recommendations"] % subject_id,
                                       start=start, count=count, _ts=ts)

    async def async_tv_recommendations(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                       ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧推荐（异步版本）
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return await self.__async_invoke_recommend(self._urls["tv_recommendations"] % subject_id,
                                                   start=start, count=count, _ts=ts)

    def movie_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影剧照
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke_search(self._urls["movie_photos"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_movie_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影剧照（异步版本）
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return await self.__async_invoke_search(self._urls["movie_photos"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def tv_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧剧照
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke_search(self._urls["tv_photos"] % subject_id,
                                    start=start, count=count, _ts=ts)

    async def async_tv_photos(self, subject_id: str, start: Optional[int] = 0, count: Optional[int] = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧剧照（异步版本）
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return await self.__async_invoke_search(self._urls["tv_photos"] % subject_id,
                                                start=start, count=count, _ts=ts)

    def person_detail(self, subject_id: int):
        """
        用户详情
        :param subject_id: 人物 id
        :return:
        """
        return self.__invoke_search(self._urls["person_detail"] + str(subject_id))

    async def async_person_detail(self, subject_id: int):
        """
        用户详情（异步版本）
        :param subject_id: 人物 id
        :return:
        """
        return await self.__async_invoke_search(self._urls["person_detail"] + str(subject_id))

    def person_work(self, subject_id: int, start: Optional[int] = 0, count: Optional[int] = 20,
                    sort_by: Optional[str] = "time",
                    collection_title: Optional[str] = "影视",
                    ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        用户作品集
        :param subject_id: work_collection id
        :param start: 开始页
        :param count: 数量
        :param sort_by: collection or time or vote
        :param collection_title: 影视 or 图书 or 音乐
        :param ts: 时间戳
        :return:
        """
        return self.__invoke_search(self._urls["person_work"] % subject_id, sortby=sort_by,
                                    collection_title=collection_title,
                                    start=start, count=count, _ts=ts)

    async def async_person_work(self, subject_id: int, start: Optional[int] = 0, count: Optional[int] = 20,
                                sort_by: Optional[str] = "time",
                                collection_title: Optional[str] = "影视",
                                ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        用户作品集（异步版本）
        :param subject_id: work_collection id
        :param start: 开始页
        :param count: 数量
        :param sort_by: collection or time or vote
        :param collection_title: 影视 or 图书 or 音乐
        :param ts: 时间戳
        :return:
        """
        return await self.__async_invoke_search(self._urls["person_work"] % subject_id, sortby=sort_by,
                                                collection_title=collection_title,
                                                start=start, count=count, _ts=ts)

    def clear_cache(self):
        """
        清空LRU缓存
        """
        self.__invoke.cache_clear()
        self.__post.cache_clear()

    def close(self):
        if self._session:
            self._session.close()
