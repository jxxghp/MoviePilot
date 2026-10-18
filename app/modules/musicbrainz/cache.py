"""
MusicBrainz 识别结果缓存、短期负缓存及文件持久化。
"""

import hashlib
import json
import pickle
import traceback
from math import ceil
from threading import RLock
from time import time
from typing import Optional, cast

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic, music_credit_values
from app.domain.music import music_isrc_codes
from app.foundation.singleton import WeakSingleton
from app.runtime.cache import CacheBackend, FileCache, TTLCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MUSIC_ENTITY_RECORDING

lock = RLock()
# 发行证据和故障语义已改变，不能继续复用旧规则身份或旧的长时间负缓存。
PERSISTENCE_VERSION = 4
PERSISTENCE_REGION = "recognize"
PERSISTENCE_KEY = "musicbrainz"


class MusicBrainzCache(metaclass=WeakSingleton):
    """
    保存音乐识别结果，并按实体范围与完整识别证据隔离缓存。

    Redis 由缓存后端持久化；其他后端只将有远端身份的结果写入文件。
    短期负缓存不落盘，重启后可以重新识别。
    """

    def __init__(self) -> None:
        """
        初始化音乐识别缓存并恢复未过期的持久化数据。
        """
        self.maxsize = get_runtime_setting('CONF').musicbrainz
        self.ttl = get_runtime_setting('CONF').meta
        self.region = f"__musicbrainz_cache_v{PERSISTENCE_VERSION}__"
        self._cache = TTLCache(region=self.region, maxsize=self.maxsize, ttl=self.ttl)
        self._expires_at: dict[str, float] = {}
        self._dirty = False
        self._file_cache = None
        if not self._cache.is_redis():
            file_cache = FileCache(base=get_runtime_setting('CACHE_PATH'), ttl=self.ttl)
            self._file_cache = file_cache
            self._restore(file_cache)

    def _restore(self, file_cache: CacheBackend) -> None:
        """
        从统一文件缓存恢复仍在有效期内的音乐识别数据。

        :param file_cache: 初始化时创建的文件缓存实例
        """
        try:
            content = file_cache.get(PERSISTENCE_KEY, region=PERSISTENCE_REGION)
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

    def _set(self, key: str, value: dict[str, object]) -> None:
        """
        无身份结果仅短暂缓存，不能让一次无匹配覆盖普通媒体缓存的整个有效期。
        """
        ttl = self.ttl if value.get("media_id") else min(self.ttl, 300)
        self._cache.set(key, value, ttl=ttl)
        if not self._cache.is_redis():
            self._expires_at[key] = time() + ttl
            self._dirty = True

    def clear(self) -> None:
        """
        清空识别结果与过期记录，并立即同步清空文件缓存。
        """
        with lock:
            self._cache.clear()
            self._expires_at.clear()
            self._dirty = True
            self.save(force=True)

    def list_items(self) -> list[dict[str, object]]:
        """
        生成管理界面可展示的音乐识别缓存列表。

        :return: 按缓存键排序的摘要列表，包含标题、艺术家、实体类型与封面
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
            return sorted(cache_items, key=lambda item: cast(str, item["key"]))

    @staticmethod
    def __get_key(meta: MetaMusic, music_type: Optional[str] = None) -> str:
        """
        按来源身份或完整识别证据及请求实体编码键，避免版本串用和分隔符碰撞。
        """
        source = str(meta.media_source) if meta.media_source else None
        identity: list[object]
        if source and meta.media_id:
            identity = ["id", source, str(meta.media_id)]
        else:
            identity = ["meta", source, meta.title, list(meta.artists or []), meta.album,
                        meta.year, meta.version, meta.isrc]
            credits = {key: value for key, value in music_credit_values(meta).items() if value}
            if credits:
                identity.append(credits)
        payload = json.dumps([music_type, identity], ensure_ascii=False, separators=(",", ":"))
        return f"[音乐:v{PERSISTENCE_VERSION}]{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def get(self, meta: MetaMusic, music_type: Optional[str] = None) -> Optional[MusicInfo]:
        """
        按音乐线索及实体范围读取已缓存的识别结果。

        :param meta: 用于构造缓存身份的音乐线索
        :param music_type: 本次实体范围；省略时与显式单曲或专辑请求隔离
        :return: 缓存中的音乐模型；未命中、已过期或解析失败时返回 None
        """
        key = self.__get_key(meta, music_type)
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

    def delete(self, key: str) -> dict[str, object]:
        """
        删除单条识别缓存，并立即同步文件缓存。

        :param key: 缓存列表返回的完整键
        :return: 被删除的原始数据；键不存在时为空映射
        """
        with lock:
            cache_data: dict[str, object] = self._cache.get(key)
            if cache_data:
                self._cache.delete(key)
                self._expires_at.pop(key, None)
                self._dirty = True
                self.save(force=True)
                return cache_data
            return {}

    def update(self, meta: MetaMusic, info: Optional[MusicInfo], music_type: Optional[str] = None) -> None:
        """
        按识别证据新增或替换缓存，缩短无远端身份结果的有效期。

        不保留完整原始响应，仅保留核验身份需要的 ISRC。
        无远端身份结果最多缓存 300 秒；文件持久化由后续 save 完成。

        :param meta: 用于构造缓存身份的音乐线索，空值不写入
        :param info: 识别结果，空值或只有服务故障、预算耗尽诊断时不写入
        :param music_type: 需要隔离的实体范围，省略表示未限定实体
        """
        if not meta or not info:
            return
        if not info.media_id and (info.raw_data.get("recognition") or {}).get("status") in {"service_error", "budget_exhausted"}:
            return
        key = self.__get_key(meta, music_type)
        cache_data = info.to_dict()
        # 原始响应不入缓存，只保留核验录音身份必需的有效ISRC集合。
        cache_data.pop("raw_data", None)
        codes = music_isrc_codes(info)
        if codes:
            cache_data["raw_data"] = {"isrcs": sorted(codes)}
        with lock:
            self._set(key, cache_data)

    def save(self, force: bool = False) -> None:
        """
        把仍有效且有远端身份的结果保存到统一文件缓存。

        Redis 或未配置文件缓存时不操作。文件保存失败只记录日志，保留待保存状态。

        :param force: 是否强制保存，False 时无变化会跳过写入
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

    def __del__(self) -> None:
        """
        实例释放前保存非 Redis 缓存。
        """
        try:
            self.save()
        except Exception:
            pass
