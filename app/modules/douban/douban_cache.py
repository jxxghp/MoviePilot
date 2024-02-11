import pickle
import random
import time
import traceback
from pathlib import Path
from threading import RLock
from typing import Optional

from app.core.config import settings
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.singleton import Singleton
from app.schemas.types import MediaType

lock = RLock()

CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"
EXPIRE_TIMESTAMP = settings.CACHE_CONF.get('meta')


class DoubanCache(metaclass=Singleton):
    """
    豆瓣缓存数据
    {
        "id": '',
        "title": '',
        "year": '',
        "type": MediaType
    }
    """
    _meta_data: dict = {}
    # 缓存文件路径
    _meta_path: Path = None
    # TMDB缓存过期
    _tmdb_cache_expire: bool = True

    def __init__(self):
        self._meta_path = settings.TEMP_PATH / "__douban_cache__"
        self._meta_data = self.__load(self._meta_path)

    def clear(self):
        """
        清空所有TMDB缓存
        """
        with lock:
            self._meta_data = {}

    @staticmethod
    def __get_key(meta: MetaBase) -> str:
        """
        获取缓存KEY
        """
        return f"[{meta.type.value if meta.type else '未知'}]" \
               f"{meta.name or meta.doubanid}-{meta.year}-{meta.begin_season}"

    def get(self, meta: MetaBase):
        """
        根据KEY值获取缓存值
        """
        key = self.__get_key(meta)
        with lock:
            info: dict = self._meta_data.get(key)
            if info:
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire or int(time.time()) < expire:
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                    self._meta_data[key] = info
                elif expire and self._tmdb_cache_expire:
                    self.delete(key)
            return info or {}

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        with lock:
            return self._meta_data.pop(key, None)

    def delete_by_doubanid(self, doubanid: str) -> None:
        """
        清空对应豆瓣ID的所有缓存记录，以强制更新TMDB中最新的数据
        """
        for key in list(self._meta_data):
            if self._meta_data.get(key, {}).get("id") == doubanid:
                with lock:
                    self._meta_data.pop(key)

    def delete_unknown(self) -> None:
        """
        清除未识别的缓存记录，以便重新搜索TMDB
        """
        for key in list(self._meta_data):
            if self._meta_data.get(key, {}).get("id") == "0":
                with lock:
                    self._meta_data.pop(key)

    def modify(self, key: str, title: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @param title: 标题
        @return: 被修改后缓存内容
        """
        with lock:
            if self._meta_data.get(key):
                self._meta_data[key]['title'] = title
                self._meta_data[key][CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
            return self._meta_data.get(key)

    @staticmethod
    def __load(path: Path) -> dict:
        """
        从文件中加载缓存
        """
        try:
            if path.exists():
                with open(path, 'rb') as f:
                    data = pickle.load(f)
                return data
            return {}
        except Exception as e:
            logger.error(f"加载缓存失败: {str(e)} - {traceback.format_exc()}")
            return {}

    def update(self, meta: MetaBase, info: dict) -> None:
        """
        新增或更新缓存条目
        """
        with lock:
            if info:
                # 缓存标题
                cache_title = info.get("title")
                # 缓存年份
                cache_year = info.get('year')
                # 类型
                if isinstance(info.get('media_type'), MediaType):
                    mtype = info.get('media_type')
                elif info.get("type"):
                    mtype = MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
                else:
                    meta = MetaInfo(cache_title)
                    if meta.begin_season:
                        mtype = MediaType.TV
                    else:
                        mtype = MediaType.MOVIE
                # 海报
                poster_path = info.get("pic", {}).get("large")
                if not poster_path and info.get("cover_url"):
                    poster_path = info.get("cover_url")
                if not poster_path and info.get("cover"):
                    poster_path = info.get("cover").get("url")

                self._meta_data[self.__get_key(meta)] = {
                        "id": info.get("id"),
                        "type": mtype,
                        "year": cache_year,
                        "title": cache_title,
                        "poster_path": poster_path,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }
            elif info is not None:
                # None时不缓存，此时代表网络错误，允许重复请求
                self._meta_data[self.__get_key(meta)] = {'id': "0"}

    def save(self, force: bool = False) -> None:
        """
        保存缓存数据到文件
        """

        meta_data = self.__load(self._meta_path)
        new_meta_data = {k: v for k, v in self._meta_data.items() if v.get("id")}

        if not force \
                and not self._random_sample(new_meta_data) \
                and meta_data.keys() == new_meta_data.keys():
            return

        with open(self._meta_path, 'wb') as f:
            pickle.dump(new_meta_data, f, pickle.HIGHEST_PROTOCOL)

    def _random_sample(self, new_meta_data: dict) -> bool:
        """
        采样分析是否需要保存
        """
        ret = False
        if len(new_meta_data) < 25:
            keys = list(new_meta_data.keys())
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._tmdb_cache_expire:
                        new_meta_data.pop(k)
        else:
            count = 0
            keys = random.sample(sorted(new_meta_data.keys()), 25)
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._tmdb_cache_expire:
                        new_meta_data.pop(k)
                        count += 1
            if count >= 5:
                ret |= self._random_sample(new_meta_data)
        return ret

    def get_title(self, key: str) -> Optional[str]:
        """
        获取缓存的标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info or not cache_media_info.get("id"):
            return None
        return cache_media_info.get("title")

    def set_title(self, key: str, cn_title: str) -> None:
        """
        重新设置缓存标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info:
            return
        self._meta_data[key]['title'] = cn_title
