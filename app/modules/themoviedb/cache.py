"""
TMDB 媒体识别缓存、类型冲突校验与本地快照迁移。
"""

import pickle
import traceback
from math import ceil
from threading import RLock
from time import time
from typing import Any, Optional

from app.domain.meta.metabase import MetaBase
from app.foundation.singleton import WeakSingleton
from app.runtime.cache import FileCache, TTLCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaSource, MediaType

lock = RLock()
# 包含独立过期时间的本地缓存快照版本。
PERSISTENCE_VERSION = 1
PERSISTENCE_REGION = "recognize"
PERSISTENCE_KEY = "tmdb"


class TmdbCache(metaclass=WeakSingleton):
    """
    管理 TMDB 识别结果、短期未命中缓存及本地持久化。

    缓存值记录媒体标识、类型、标题、年份和主图片路径。Redis 后端自行持久化；
    本地后端保存未过期的有效识别结果，不保存未命中条目。
    """

    def __init__(self) -> None:
        """
        初始化 TMDB 识别缓存并恢复未过期的持久化数据。
        """
        self.maxsize = get_runtime_setting('CONF').tmdb
        self.ttl = get_runtime_setting('CONF').meta
        self.region = "__tmdb_cache__"
        self._cache = TTLCache(region=self.region, maxsize=self.maxsize, ttl=self.ttl)
        self._expires_at: dict[str, float] = {}
        self._dirty = False
        self._file_cache = None
        self._legacy_file_cache = None
        self._legacy_cache_found = False
        if not self._cache.is_redis():
            self._file_cache = FileCache(base=get_runtime_setting('CACHE_PATH'), ttl=self.ttl)
            self._legacy_file_cache = FileCache(base=get_runtime_setting('TEMP_PATH').parent, ttl=self.ttl)
            self._restore()

    def _restore(self) -> None:
        """
        从统一文件缓存恢复仍在有效期内的 TMDB 识别数据。
        """
        if self._file_cache is None or self._legacy_file_cache is None:
            return
        try:
            content = self._file_cache.get(PERSISTENCE_KEY, region=PERSISTENCE_REGION)
            if not content:
                content = self._legacy_file_cache.get(
                    self.region,
                    region=get_runtime_setting('TEMP_PATH').name,
                )
                if content:
                    self._legacy_cache_found = True
                    self._dirty = True
            if not content:
                return
            payload = pickle.loads(content)
            now = time()
            if (
                    isinstance(payload, dict)
                    and payload.get("version") == PERSISTENCE_VERSION
                    and isinstance(payload.get("items"), dict)
            ):
                items = payload["items"]
            elif isinstance(payload, dict):
                # 旧版缓存没有保存过期时间，迁移时从当前时刻重新计算一次有效期。
                items = {
                    key: {"value": value, "expires_at": now + self.ttl}
                    for key, value in payload.items()
                }
                self._dirty = True
            else:
                return

            for key, item in items.items():
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
            logger.error(f"加载TMDB识别缓存失败：{str(err)} - {traceback.format_exc()}")

    def _set(self, key: str, value: dict[str, Any], ttl: Optional[int] = None) -> None:
        """
        写入单条 TMDB 识别缓存并记录其独立过期时间，未指定 ttl 时用默认有效期。
        """
        ttl = self.ttl if ttl is None else ttl
        self._cache.set(key, value, ttl=ttl)
        if not self._cache.is_redis():
            self._expires_at[key] = time() + ttl
            self._dirty = True

    def clear(self) -> None:
        """
        清空识别缓存和过期记录，并立即同步到本地持久化。
        """
        with lock:
            self._cache.clear()
            self._expires_at.clear()
            self._dirty = True
            self.save(force=True)

    def list_items(self) -> list[dict[str, Any]]:
        """
        生成按缓存键排序的管理界面条目列表。

        :return: 包含媒体标识、标题、年份、类型和图片地址的字典列表；无效类型标为 unknown
        """
        with lock:
            cache_items = []
            for key, value in self._cache.items():
                if not isinstance(value, dict):
                    continue
                media_type = value.get("type")
                if not isinstance(media_type, MediaType):
                    try:
                        media_type = MediaType(media_type)
                    except (TypeError, ValueError):
                        media_type = None
                cache_items.append({
                    "key": key,
                    "tmdb_id": value.get("id") or 0,
                    "title": value.get("title") or "",
                    "year": value.get("year") or "",
                    "media_type": media_type.to_agent() if media_type else "unknown",
                    "poster_path": value.get("poster_path") or "",
                    "backdrop_path": value.get("backdrop_path") or "",
                })
            return sorted(cache_items, key=lambda item: item["key"])

    @staticmethod
    def __get_key(meta: MetaBase) -> str:
        """
        按媒体类型、语言、TMDB 标识或名称、年份及起始季生成识别缓存键。
        """
        media_id = meta.media_id if meta.media_source == MediaSource.TMDB else None
        return f"[{meta.type.value if meta.type else '未知'}][{get_runtime_setting('TMDB_LOCALE')}]{media_id or meta.name}-{meta.year}-{meta.begin_season}"

    @staticmethod
    def __is_type_conflicted(meta: MetaBase, media_type: Any, tmdb_id: Any) -> bool:
        """
        判断媒体类型是否与元数据声明的类型冲突。

        只有「元数据判定为电视剧、结果却是电影」才算冲突。反向不算：名称识别在
        电影分支查不到时会回退到电视剧查询，识别缓存正是用来记住这个纠正结果，
        一律要求 key 与 value 类型一致会让这类条目每次都被丢弃、反复回源。而电视
        剧分支恒定写入电视剧类型，`[电视剧]` 键下出现电影只可能来自 tmdbid 消歧
        或共享识别回填的脏写，会让整季剧集被当成电影反复整理失败。

        :param meta: 元数据
        :param media_type: 待校验的媒体类型
        :param tmdb_id: 对应的 TMDB ID，为空表示负缓存，不带类型信息
        :return: 是否冲突
        """
        if meta.type != MediaType.TV or not tmdb_id:
            return False
        if not isinstance(media_type, MediaType):
            try:
                media_type = MediaType(media_type)
            except (TypeError, ValueError):
                return False
        return bool(media_type == MediaType.MOVIE)

    def get(self, meta: MetaBase) -> dict[str, Any]:
        """
        读取元数据对应的有效识别缓存，并剔除类型冲突条目。

        :param meta: 待识别媒体元数据，用于生成与当前语言相关的缓存键
        :return: 缓存条目，id 为 0 表示短期未命中；缺失、无效或冲突时返回空字典
        """
        key = self.__get_key(meta)

        with lock:
            cache_data = self._cache.get(key)
            if not cache_data and self._expires_at.pop(key, None) is not None:
                self._dirty = True
            if not isinstance(cache_data, dict):
                return {}
            if not cache_data:
                return {}
            if self.__is_type_conflicted(meta, cache_data.get("type"), cache_data.get("id")):
                # 脏条目不丢弃就会被无限期沿用，正确的识别逻辑永远没有执行机会
                logger.warn(f"识别缓存类型与元数据冲突，已丢弃并重新识别：{key} -> "
                            f"{cache_data.get('title')}({cache_data.get('type')})")
                self._cache.delete(key)
                self._expires_at.pop(key, None)
                self._dirty = True
                return {}
            return cache_data

    def delete(self, key: str) -> dict[str, Any]:
        """
        删除指定识别缓存并立即保存本地持久化状态。

        :param key: 管理列表提供的完整缓存键
        :return: 被删除的条目；不存在时返回空字典
        """
        with lock:
            redis_data = self._cache.get(key)
            if isinstance(redis_data, dict) and redis_data:
                self._cache.delete(key)
                self._expires_at.pop(key, None)
                self._dirty = True
                self.save(force=True)
                return redis_data
            return {}

    def modify(self, key: str, title: str) -> dict[str, Any]:
        """
        修改已有缓存标题，并重置该条目的默认有效期。

        :param key: 管理列表提供的完整缓存键
        :param title: 替换后的展示标题
        :return: 更新后的条目；不存在时返回空字典
        """
        with lock:
            redis_data = self._cache.get(key)
            if isinstance(redis_data, dict) and redis_data:
                redis_data['title'] = title
                self._set(key, redis_data)
                return redis_data
            return {}

    def update(self, meta: MetaBase, info: Optional[dict[str, Any]]) -> None:
        """
        从 TMDB 结果更新识别缓存，区分未命中和网络失败。

        拒绝把电影结果写入已判定为电视剧的键；电影元数据回退识别为电视剧时允许缓存。

        :param meta: 用于生成缓存键和检查媒体类型的元数据
        :param info: TMDB 识别结果；空字典记录短期未命中，None 表示失败且不缓存
        """
        key = self.__get_key(meta)
        if info:
            if self.__is_type_conflicted(meta, info.get("media_type"), info.get("id")):
                # 拒绝写入而不是改写键：识别结果照常返回，只是不把矛盾条目留给下一次
                logger.warn(f"识别结果类型与元数据冲突，不写入识别缓存：{key} -> "
                            f"{info.get('title')}({info.get('media_type')})")
                return
            cache_title = info.get("title") \
                if info.get("media_type") == MediaType.MOVIE else info.get("name")
            cache_year = info.get('release_date') \
                if info.get("media_type") == MediaType.MOVIE else info.get('first_air_date')
            if cache_year:
                cache_year = cache_year[:4]

            with lock:
                cache_data = {
                    "id": info.get("id"),
                    "type": info.get("media_type"),
                    "year": cache_year,
                    "title": cache_title,
                    "poster_path": info.get("poster_path"),
                    "backdrop_path": info.get("backdrop_path")
                }
                self._set(key, cache_data)

        elif info is not None:
            # None时不缓存，此时代表网络错误，允许重复请求
            with lock:
                # 负识别缓存使用独立的短 TTL：故障期间「合法 JSON 但结果为空」会被
                # 记为未识别，若按完整有效期固化，故障自愈后同名仍会被判无法识别；
                # 短过期让恢复后可重新识别，真不存在的条目过期后重新确认一次即可
                self._set(key, {"id": 0}, ttl=get_runtime_setting('EMPTY_RESULT_CACHE_TTL'))

    def save(self, force: bool = False) -> None:
        """
        把本地后端仍有效且有媒体标识的缓存写入统一文件区域。

        Redis 后端直接返回；保存成功后移除已发现的旧版缓存。文件操作失败仅记录日志。

        :param force: True 时即使未标记变更也执行保存
        """
        if self._cache.is_redis():
            return
        if self._file_cache is None or self._legacy_file_cache is None:
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
                if expires_at <= now or not value.get("id"):
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
                if self._legacy_cache_found:
                    self._legacy_file_cache.delete(
                        self.region,
                        region=get_runtime_setting('TEMP_PATH').name,
                    )
                    self._legacy_cache_found = False
                self._dirty = False
            except Exception as err:
                logger.error(f"保存TMDB识别缓存失败：{str(err)} - {traceback.format_exc()}")

    def __del__(self) -> None:
        """
        实例释放前保存非 Redis 缓存。
        """
        self.save()
