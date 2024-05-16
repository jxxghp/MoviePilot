# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
from datetime import datetime
from functools import lru_cache
from random import choice
from urllib import parse

import requests

from app.core.config import settings
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class DoubanApi(metaclass=Singleton):
    _urls = {
        # 搜索类
        # sort=U:近期热门 T:标记最多 S:评分最高 R:最新上映
        # q=search_word&start: int = 0&count: int = 20&sort=U
        # 聚合搜索
        "search": "/search/weixin",
        "search_agg": "/search",
        "search_subject": "/search/subjects",
        "imdbid": "/movie/imdb/%s",

        # 电影探索
        # sort=U:综合排序 T:近期热度 S:高分优先 R:首播时间
        # tags='日本,动画,2022'&start: int = 0&count: int = 20&sort=U
        "movie_recommend": "/movie/recommend",
        # 电视剧探索
        "tv_recommend": "/tv/recommend",
        # 搜索
        "movie_tag": "/movie/tag",
        "tv_tag": "/tv/tag",
        # q=search_word&start: int = 0&count: int = 20
        "movie_search": "/search/movie",
        "tv_search": "/search/movie",
        "book_search": "/search/book",
        "group_search": "/search/group",

        # 各类主题合集
        # start: int = 0&count: int = 20
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
    _session = None

    def __init__(self):
        self._session = requests.Session()

    @classmethod
    def __sign(cls, url: str, ts: int, method='GET') -> str:
        """
        签名
        """
        url_path = parse.urlparse(url).path
        raw_sign = '&'.join([method.upper(), parse.quote(url_path, safe=''), str(ts)])
        return base64.b64encode(
            hmac.new(
                cls._api_secret_key.encode(),
                raw_sign.encode(),
                hashlib.sha1
            ).digest()
        ).decode()

    @lru_cache(maxsize=settings.CACHE_CONF.get('douban'))
    def __invoke(self, url: str, **kwargs) -> dict:
        """
        GET请求
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
        resp = RequestUtils(
            ua=choice(self._user_agents),
            session=self._session
        ).get_res(url=req_url, params=params)
        if resp is not None and resp.status_code == 400 and "rate_limit" in resp.text:
            return resp.json()
        return resp.json() if resp else {}

    @lru_cache(maxsize=settings.CACHE_CONF.get('douban'))
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
        req_url = self._api_url + url
        params = {'apikey': self._api_key2}
        if kwargs:
            params.update(kwargs)
        if '_ts' in params:
            params.pop('_ts')
        resp = RequestUtils(
            ua=settings.USER_AGENT,
            session=self._session,
        ).post_res(url=req_url, data=params)
        if resp is not None and resp.status_code == 400 and "rate_limit" in resp.text:
            return resp.json()
        return resp.json() if resp else {}

    def imdbid(self, imdbid: str,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        IMDBID搜索
        """
        return self.__post(self._urls["imdbid"] % imdbid, _ts=ts)

    def search(self, keyword: str, start: int = 0, count: int = 20,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')) -> dict:
        """
        关键字搜索
        """
        return self.__invoke(self._urls["search"], q=keyword,
                             start=start, count=count, _ts=ts)

    def movie_search(self, keyword: str, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影搜索
        """
        return self.__invoke(self._urls["movie_search"], q=keyword,
                             start=start, count=count, _ts=ts)

    def tv_search(self, keyword: str, start: int = 0, count: int = 20,
                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视搜索
        """
        return self.__invoke(self._urls["tv_search"], q=keyword,
                             start=start, count=count, _ts=ts)

    def book_search(self, keyword: str, start: int = 0, count: int = 20,
                    ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        书籍搜索
        """
        return self.__invoke(self._urls["book_search"], q=keyword,
                             start=start, count=count, _ts=ts)

    def group_search(self, keyword: str, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        小组搜索
        """
        return self.__invoke(self._urls["group_search"], q=keyword,
                             start=start, count=count, _ts=ts)

    def person_search(self, keyword: str, start: int = 0, count: int = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        人物搜索
        """
        return self.__invoke(self._urls["search_subject"], type="person", q=keyword,
                             start=start, count=count, _ts=ts)

    def movie_showing(self, start: int = 0, count: int = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        正在热映
        """
        return self.__invoke(self._urls["movie_showing"],
                             start=start, count=count, _ts=ts)

    def movie_soon(self, start: int = 0, count: int = 20,
                   ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        即将上映
        """
        return self.__invoke(self._urls["movie_soon"],
                             start=start, count=count, _ts=ts)

    def movie_hot_gaia(self, start: int = 0, count: int = 20,
                       ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门电影
        """
        return self.__invoke(self._urls["movie_hot_gaia"],
                             start=start, count=count, _ts=ts)

    def tv_hot(self, start: int = 0, count: int = 20,
               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        热门剧集
        """
        return self.__invoke(self._urls["tv_hot"],
                             start=start, count=count, _ts=ts)

    def tv_animation(self, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        动画
        """
        return self.__invoke(self._urls["tv_animation"],
                             start=start, count=count, _ts=ts)

    def tv_variety_show(self, start: int = 0, count: int = 20,
                        ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺
        """
        return self.__invoke(self._urls["tv_variety_show"],
                             start=start, count=count, _ts=ts)

    def tv_rank_list(self, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧排行榜
        """
        return self.__invoke(self._urls["tv_rank_list"],
                             start=start, count=count, _ts=ts)

    def show_hot(self, start: int = 0, count: int = 20,
                 ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        综艺热门
        """
        return self.__invoke(self._urls["show_hot"],
                             start=start, count=count, _ts=ts)

    def movie_detail(self, subject_id: str):
        """
        电影详情
        """
        return self.__invoke(self._urls["movie_detail"] + subject_id)

    def movie_celebrities(self, subject_id: str):
        """
        电影演职员
        """
        return self.__invoke(self._urls["movie_celebrities"] % subject_id)

    def tv_detail(self, subject_id: str):
        """
        电视剧详情
        """
        return self.__invoke(self._urls["tv_detail"] + subject_id)

    def tv_celebrities(self, subject_id: str):
        """
        电视剧演职员
        """
        return self.__invoke(self._urls["tv_celebrities"] % subject_id)

    def book_detail(self, subject_id: str):
        """
        书籍详情
        """
        return self.__invoke(self._urls["book_detail"] + subject_id)

    def movie_top250(self, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影TOP250
        """
        return self.__invoke(self._urls["movie_top250"],
                             start=start, count=count, _ts=ts)

    def movie_recommend(self, tags='', sort='R', start: int = 0, count: int = 20,
                        ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影探索
        """
        return self.__invoke(self._urls["movie_recommend"], tags=tags, sort=sort,
                             start=start, count=count, _ts=ts)

    def tv_recommend(self, tags='', sort='R', start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧探索
        """
        return self.__invoke(self._urls["tv_recommend"], tags=tags, sort=sort,
                             start=start, count=count, _ts=ts)

    def tv_chinese_best_weekly(self, start: int = 0, count: int = 20,
                               ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        华语口碑周榜
        """
        return self.__invoke(self._urls["tv_chinese_best_weekly"],
                             start=start, count=count, _ts=ts)

    def tv_global_best_weekly(self, start: int = 0, count: int = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        全球口碑周榜
        """
        return self.__invoke(self._urls["tv_global_best_weekly"],
                             start=start, count=count, _ts=ts)

    def doulist_detail(self, subject_id: str):
        """
        豆列详情
        :param subject_id: 豆列id
        """
        return self.__invoke(self._urls["doulist"] + subject_id)

    def doulist_items(self, subject_id: str, start: int = 0, count: int = 20,
                      ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        豆列列表
        :param subject_id: 豆列id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke(self._urls["doulist_items"] % subject_id,
                             start=start, count=count, _ts=ts)

    def movie_recommendations(self, subject_id: str, start: int = 0, count: int = 20,
                              ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影推荐
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke(self._urls["movie_recommendations"] % subject_id,
                             start=start, count=count, _ts=ts)

    def tv_recommendations(self, subject_id: str, start: int = 0, count: int = 20,
                           ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧推荐
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke(self._urls["tv_recommendations"] % subject_id,
                             start=start, count=count, _ts=ts)

    def movie_photos(self, subject_id: str, start: int = 0, count: int = 20,
                     ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电影剧照
        :param subject_id: 电影id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke(self._urls["movie_photos"] % subject_id,
                             start=start, count=count, _ts=ts)

    def tv_photos(self, subject_id: str, start: int = 0, count: int = 20,
                  ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        电视剧剧照
        :param subject_id: 电视剧id
        :param start: 开始
        :param count: 数量
        :param ts: 时间戳
        """
        return self.__invoke(self._urls["tv_photos"] % subject_id,
                             start=start, count=count, _ts=ts)

    def person_detail(self, subject_id: int):
        """
        用户详情
        :param subject_id: 人物 id
        :return:
        """
        return self.__invoke(self._urls["person_detail"] + str(subject_id))

    def person_work(self, subject_id: int, start: int = 0, count: int = 20, sort_by: str = "time",
                    collection_title: str = "影视",
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
        return self.__invoke(self._urls["person_work"] % subject_id, sortby=sort_by, collection_title=collection_title,
                             start=start, count=count, _ts=ts)

    def clear_cache(self):
        """
        清空LRU缓存
        """
        self.__invoke.cache_clear()

    def close(self):
        if self._session:
            self._session.close()
