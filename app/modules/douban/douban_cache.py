import pickle
import traceback
from pathlib import Path
from threading import RLock
from typing import Optional

from app.core.cache import TTLCache
from app.core.config import settings
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import MediaType
from app.utils.singleton import WeakSingleton

lock = RLock()


class DoubanCache(metaclass=WeakSingleton):
    """
    豆瓣缓存数据
    {
        "id": '',
        "title": '',
        "year": '',
        "type": MediaType
    }
    """
    # TMDB缓存过期
    _douban_cache_expire: bool = True

    def __init__(self):
        self.maxsize = settings.CONF.douban
        self.ttl = settings.CONF.meta
        self.region = "__douban_cache__"
        self._meta_filepath = settings.TEMP_PATH / self.region
        # 初始化缓存
        self._cache = TTLCache(region=self.region, maxsize=self.maxsize, ttl=self.ttl)
        # 非Redis加载本地缓存数据
        if not self._cache.is_redis():
            for key, value in self.__load(self._meta_filepath).items():
                self._cache.set(key, value)

    def clear(self):
        """
        清空所有豆瓣缓存
        """
        with lock:
            self._cache.clear()

    @staticmethod
    def __get_key(meta: MetaBase) -> str:
        """
        获取缓存KEY
        """
        return f"[{meta.type.value if meta.type else '未知'}]" \
               f"{meta.doubanid or meta.name}-{meta.year}-{meta.begin_season}"

    def get(self, meta: MetaBase):
        """
        根据KEY值获取缓存值
        """
        key = self.__get_key(meta)
        with lock:
            return self._cache.get(key) or {}

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        with lock:
            redis_data = self._cache.get(key)
            if redis_data:
                self._cache.delete(key)
                return redis_data
            return {}

    def modify(self, key: str, title: str) -> dict:
        """
        修改缓存信息
        @param key: 缓存key
        @param title: 标题
        @return: 被修改后缓存内容
        """
        with lock:
            redis_data = self._cache.get(key)
            if redis_data:
                redis_data["title"] = title
                self._cache.set(key, redis_data)
                return redis_data
            return {}

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
        except Exception as e:
            logger.error(f"加载缓存失败: {str(e)} - {traceback.format_exc()}")
        return {}

    def update(self, meta: MetaBase, info: dict) -> None:
        """
        新增或更新缓存条目
        """
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

            with lock:
                self._cache.set(self.__get_key(meta), {
                    "id": info.get("id"),
                    "type": mtype,
                    "year": cache_year,
                    "title": cache_title,
                    "poster_path": poster_path
                })

        elif info is not None:
            # None时不缓存，此时代表网络错误，允许重复请求
            with lock:
                self._cache.set(self.__get_key(meta), {
                    "id": 0
                })

    def save(self, force: Optional[bool] = False) -> None:
        """
        保存缓存数据到文件
        """
        # Redis不需要保存到本地文件
        if self._cache.is_redis():
            return

        # 本地文件
        meta_data = self.__load(self._meta_filepath)
        # 当前缓存数据（去除无法识别）
        new_meta_data = {k: v for k, v in self._cache.items() if v.get("id")}

        if not force \
                and meta_data.keys() == new_meta_data.keys():
            return
        # 写入本地
        with open(self._meta_filepath, 'wb') as f:
            pickle.dump(new_meta_data, f, pickle.HIGHEST_PROTOCOL)  # noqa

    def __del__(self):
        self.save()
