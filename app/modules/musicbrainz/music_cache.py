import pickle
import traceback
from math import ceil
from threading import RLock
from time import time
from typing import Optional

from app.runtime.cache import FileCache, TTLCache
from app.runtime.config import settings
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_RECORDING
from app.foundation.singleton import WeakSingleton

lock = RLock()
PERSISTENCE_VERSION = 1
PERSISTENCE_REGION = "recognize"
PERSISTENCE_KEY = "musicbrainz"


class MusicBrainzCache(metaclass=WeakSingleton):
    """
    MusicBrainz识别缓存数据
    {
        "source": '',
        "media_id": '',
        "title": '',
        "artists": [],
        "album": '',
        "year": '',
        "music_type": ''
    }
    """

    def __init__(self):
        """初始化音乐识别缓存并恢复未过期的持久化数据。"""
        self.maxsize = settings.CONF.musicbrainz
        self.ttl = settings.CONF.meta
        self.region = "__musicbrainz_cache__"
        self._cache = TTLCache(region=self.region, maxsize=self.maxsize, ttl=self.ttl)
        self._expires_at: dict[str, float] = {}
        self._dirty = False
        self._file_cache = None
        if not self._cache.is_redis():
            self._file_cache = FileCache(base=settings.CACHE_PATH, ttl=self.ttl)
            self._restore()

    def _restore(self) -> None:
        """从统一文件缓存恢复仍在有效期内的音乐识别数据。"""
        try:
            content = self._file_cache.get(PERSISTENCE_KEY, region=PERSISTENCE_REGION)
            if not content:
                return
            payload = pickle.loads(content)
            now = time()
            if (
                    not isinstance(payload, dict)
                    or payload.get("version") != PERSISTENCE_VERSION
                    or not isinstance(payload.get("items"), dict)
            ):
                return

            for key, item in payload["items"].items():
                if not isinstance(item, dict):
                    self._dirty = True
                    continue
                value = item.get("value")
                expires_at = item.get("expires_at")
                if not isinstance(value, dict) or not isinstance(expires_at, (int, float)):
                    self._dirty = True
                    continue
                remaining_ttl = expires_at - now
                if remaining_ttl <= 0:
                    self._dirty = True
                    continue
                self._cache.set(key, value, ttl=ceil(remaining_ttl))
                self._expires_at[key] = expires_at
        except Exception as err:
            logger.error(f"加载音乐识别缓存失败：{str(err)} - {traceback.format_exc()}")

    def _set(self, key: str, value: dict) -> None:
        """写入单条音乐识别缓存并记录其独立过期时间。"""
        self._cache.set(key, value)
        if not self._cache.is_redis():
            self._expires_at[key] = time() + self.ttl
            self._dirty = True

    def clear(self):
        """
        清空所有音乐识别缓存
        """
        with lock:
            self._cache.clear()
            self._expires_at.clear()
            self._dirty = True
            self.save(force=True)

    def list_items(self) -> list[dict]:
        """
        返回可供管理界面展示的音乐识别缓存列表。
        """
        with lock:
            cache_items = []
            for key, value in self._cache.items():
                if not isinstance(value, dict):
                    continue
                cache_items.append({
                    "key": key,
                    "media_id": value.get("media_id") or "",
                    "title": value.get("title") or "",
                    "artists": value.get("artists") or [],
                    "album": value.get("album") or "",
                    "year": value.get("year") or "",
                    "music_type": value.get("music_type") or MUSIC_ENTITY_RECORDING,
                    "cover_url": value.get("cover_url") or "",
                })
            return sorted(cache_items, key=lambda item: item["key"])

    @staticmethod
    def __get_key(meta: MetaMusic) -> str:
        """
        获取缓存KEY，携带数据源原生 ID 时以 ID 为准身份
        """
        artists = "/".join(meta.artists or [])
        return f"[音乐]{meta.media_id or meta.title}-{artists}-{meta.album}-{meta.year}"

    def get(self, meta: MetaMusic) -> Optional[MusicInfo]:
        """
        根据元数据获取缓存的音乐识别结果
        @param meta: 音乐元数据
        @return: 缓存命中的音乐信息，未命中返回 None
        """
        key = self.__get_key(meta)
        with lock:
            cache_data = self._cache.get(key)
            if not cache_data and self._expires_at.pop(key, None) is not None:
                self._dirty = True
        if not cache_data:
            return None
        try:
            return MusicInfo.from_dict(cache_data)
        except Exception as err:
            logger.error(f"解析音乐识别缓存失败：{str(err)}")
            return None

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        with lock:
            cache_data = self._cache.get(key)
            if cache_data:
                self._cache.delete(key)
                self._expires_at.pop(key, None)
                self._dirty = True
                self.save(force=True)
                return cache_data
            return {}

    def update(self, meta: MetaMusic, info: Optional[MusicInfo]) -> None:
        """
        新增或更新缓存条目，无远端身份的兜底结果也写入内存负缓存，
        避免批量识别时反复请求 MusicBrainz 触发限流
        """
        if not meta or not info:
            return
        key = self.__get_key(meta)
        cache_data = info.to_dict()
        # 上游原始响应体积大且不参与身份恢复，不入缓存
        cache_data.pop("raw_data", None)
        with lock:
            self._set(key, cache_data)

    def save(self, force: bool = False) -> None:
        """
        使用统一文件缓存保存未过期的音乐识别数据。
        """
        if self._cache.is_redis():
            return
        if not self._file_cache:
            return
        with lock:
            now = time()
            cache_items = dict(self._cache.items())
            active_keys = set(cache_items)
            stale_keys = set(self._expires_at) - active_keys
            if stale_keys:
                for key in stale_keys:
                    self._expires_at.pop(key, None)
                self._dirty = True

            persisted_items = {}
            for key, value in cache_items.items():
                expires_at = self._expires_at.get(key)
                if expires_at is None:
                    expires_at = now + self.ttl
                    self._expires_at[key] = expires_at
                    self._dirty = True
                # 负缓存只留在内存，重启后允许重新尝试识别
                if expires_at <= now or not value.get("media_id"):
                    continue
                persisted_items[key] = {
                    "value": value,
                    "expires_at": expires_at,
                }

            if not force and not self._dirty:
                return

            try:
                if persisted_items:
                    payload = {
                        "version": PERSISTENCE_VERSION,
                        "items": persisted_items,
                    }
                    self._file_cache.set(
                        PERSISTENCE_KEY,
                        pickle.dumps(payload, pickle.HIGHEST_PROTOCOL),
                        region=PERSISTENCE_REGION,
                    )
                else:
                    self._file_cache.delete(PERSISTENCE_KEY, region=PERSISTENCE_REGION)
                self._dirty = False
            except Exception as err:
                logger.error(f"保存音乐识别缓存失败：{str(err)} - {traceback.format_exc()}")

    def __del__(self):
        """实例释放前保存非 Redis 缓存。"""
        try:
            self.save()
        except Exception:
            pass
