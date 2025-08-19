import pickle
import time
import traceback
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

from app.core.config import settings
from app.log import logger
from app.helper.redis_helper import redis_helper


class CacheManager:
    """
    统一的缓存管理器，支持Redis和本地文件缓存的混合使用
    优先使用Redis缓存，Redis不可用时回退到本地文件缓存
    """
    
    def __init__(self, cache_name: str, region: str = "DEFAULT"):
        """
        初始化缓存管理器
        
        :param cache_name: 缓存名称，用于本地文件缓存
        :param region: 缓存区域，用于Redis缓存
        """
        self.cache_name = cache_name
        self.region = region
        self._local_cache_path = settings.TEMP_PATH / f"__{cache_name}__"
        self._local_cache_data = {}
        self._lock = RLock()
        self._load_local_cache()
    
    def _load_local_cache(self):
        """
        从本地文件加载缓存数据
        """
        try:
            if self._local_cache_path.exists():
                with open(self._local_cache_path, 'rb') as f:
                    self._local_cache_data = pickle.load(f)
                logger.debug(f"Loaded local cache from {self._local_cache_path}")
            else:
                self._local_cache_data = {}
        except Exception as e:
            logger.error(f"Failed to load local cache: {str(e)} - {traceback.format_exc()}")
            self._local_cache_data = {}
    
    def _save_local_cache(self, force: bool = False):
        """
        保存缓存数据到本地文件
        
        :param force: 是否强制保存
        """
        try:
            # 过滤掉无效的缓存项，但保留种子缓存等不需要id字段的数据
            if self.cache_name.startswith("torrents_"):
                # 种子缓存不需要过滤
                valid_cache = self._local_cache_data
            else:
                # 其他缓存需要过滤
                valid_cache = {k: v for k, v in self._local_cache_data.items() 
                              if v and v.get("id") and v.get("id") != 0 and v.get("id") != "0"}
            
            if not force and len(valid_cache) == 0:
                return
                
            with open(self._local_cache_path, 'wb') as f:
                pickle.dump(valid_cache, f, pickle.HIGHEST_PROTOCOL)
            logger.debug(f"Saved local cache to {self._local_cache_path}")
        except Exception as e:
            logger.error(f"Failed to save local cache: {str(e)} - {traceback.format_exc()}")
    
    def _get_cache_key(self, key: str) -> str:
        """
        获取缓存键
        """
        return f"{self.cache_name}:{key}"
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取缓存值
        
        :param key: 缓存键
        :param default: 默认值
        :return: 缓存值或默认值
        """
        cache_key = self._get_cache_key(key)
        
        # 首先尝试从Redis获取
        try:
            redis_value = redis_helper.get(cache_key, region=self.region)
            if redis_value is not None:
                logger.debug(f"Cache hit from Redis: {cache_key}")
                return redis_value
        except Exception as e:
            logger.debug(f"Redis cache miss for {cache_key}: {e}")
        
        # Redis没有数据，尝试从本地文件获取
        with self._lock:
            local_value = self._local_cache_data.get(key)
            if local_value is not None:
                # 对于种子缓存，直接返回数据
                if self.cache_name.startswith("torrents_"):
                    logger.debug(f"Cache hit from local file: {key}")
                    return local_value
                else:
                    # 其他缓存检查过期时间
                    expire_time = local_value.get("expire_time")
                    if expire_time is None or time.time() < expire_time:
                        logger.debug(f"Cache hit from local file: {key}")
                        return local_value.get("data")
                    else:
                        # 缓存已过期，删除
                        self._local_cache_data.pop(key, None)
        
        return default
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        设置缓存值
        
        :param key: 缓存键
        :param value: 缓存值
        :param ttl: 过期时间（秒）
        """
        cache_key = self._get_cache_key(key)
        ttl = ttl or settings.CONF.meta * 3600  # 默认使用配置的元数据缓存时间
        
        # 同时设置Redis和本地缓存
        try:
            # 设置Redis缓存
            redis_helper.set(cache_key, value, ttl=ttl, region=self.region)
            logger.debug(f"Set Redis cache: {cache_key}")
        except Exception as e:
            logger.debug(f"Failed to set Redis cache for {cache_key}: {e}")
        
        # 设置本地缓存
        with self._lock:
            if self.cache_name.startswith("torrents_"):
                # 种子缓存直接存储数据
                self._local_cache_data[key] = value
            else:
                # 其他缓存存储带过期时间的数据
                self._local_cache_data[key] = {
                    "data": value,
                    "expire_time": time.time() + ttl if ttl else None
                }
            self._save_local_cache()
            logger.debug(f"Set local cache: {key}")
    
    def delete(self, key: str) -> None:
        """
        删除缓存
        
        :param key: 缓存键
        """
        cache_key = self._get_cache_key(key)
        
        # 删除Redis缓存
        try:
            redis_helper.delete(cache_key, region=self.region)
            logger.debug(f"Deleted Redis cache: {cache_key}")
        except Exception as e:
            logger.debug(f"Failed to delete Redis cache for {cache_key}: {e}")
        
        # 删除本地缓存
        with self._lock:
            if key in self._local_cache_data:
                del self._local_cache_data[key]
                self._save_local_cache()
                logger.debug(f"Deleted local cache: {key}")
    
    def exists(self, key: str) -> bool:
        """
        检查缓存是否存在
        
        :param key: 缓存键
        :return: 是否存在
        """
        cache_key = self._get_cache_key(key)
        
        # 检查Redis缓存
        try:
            if redis_helper.exists(cache_key, region=self.region):
                return True
        except Exception as e:
            logger.debug(f"Failed to check Redis cache for {cache_key}: {e}")
        
        # 检查本地缓存
        with self._lock:
            local_value = self._local_cache_data.get(key)
            if local_value is not None:
                # 对于种子缓存，直接返回True
                if self.cache_name.startswith("torrents_"):
                    return True
                else:
                    # 其他缓存检查过期时间
                    expire_time = local_value.get("expire_time")
                    if expire_time is None or time.time() < expire_time:
                        return True
                    else:
                        # 缓存已过期，删除
                        self._local_cache_data.pop(key, None)
        
        return False
    
    def clear(self) -> None:
        """
        清空所有缓存
        """
        # 清空Redis缓存
        try:
            redis_helper.clear(region=self.region)
            logger.info(f"Cleared Redis cache for region: {self.region}")
        except Exception as e:
            logger.debug(f"Failed to clear Redis cache for region {self.region}: {e}")
        
        # 清空本地缓存
        with self._lock:
            self._local_cache_data.clear()
            self._save_local_cache(force=True)
            logger.info(f"Cleared local cache: {self.cache_name}")
    
    def get_all(self) -> Dict[str, Any]:
        """
        获取所有缓存数据
        
        :return: 所有缓存数据
        """
        result = {}
        
        # 获取本地缓存数据
        with self._lock:
            current_time = time.time()
            for key, value in self._local_cache_data.items():
                if self.cache_name.startswith("torrents_"):
                    # 种子缓存直接返回数据
                    result[key] = value
                else:
                    # 其他缓存检查过期时间
                    expire_time = value.get("expire_time")
                    if expire_time is None or current_time < expire_time:
                        result[key] = value.get("data")
        
        return result
    
    def update(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        更新缓存值（如果存在的话）
        
        :param key: 缓存键
        :param value: 缓存值
        :param ttl: 过期时间（秒）
        """
        if self.exists(key):
            self.set(key, value, ttl)
    
    def __del__(self):
        """
        析构函数，保存本地缓存
        """
        try:
            self._save_local_cache()
        except:
            pass


class TmdbCacheManager(CacheManager):
    """
    TMDB缓存管理器
    """
    
    def __init__(self):
        super().__init__("tmdb_cache", region="themoviedb")
    
    def get_by_meta(self, meta) -> Dict[str, Any]:
        """
        根据元数据获取缓存
        
        :param meta: 元数据对象
        :return: 缓存数据
        """
        key = f"[{meta.type.value if meta.type else '未知'}]{meta.tmdbid or meta.name}-{meta.year}-{meta.begin_season}"
        return self.get(key, {})
    
    def set_by_meta(self, meta, info: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """
        根据元数据设置缓存
        
        :param meta: 元数据对象
        :param info: 缓存数据
        :param ttl: 过期时间（秒）
        """
        key = f"[{meta.type.value if meta.type else '未知'}]{meta.tmdbid or meta.name}-{meta.year}-{meta.begin_season}"
        self.set(key, info, ttl)
    
    def delete_by_tmdbid(self, tmdbid: int) -> None:
        """
        根据TMDB ID删除缓存
        
        :param tmdbid: TMDB ID
        """
        all_data = self.get_all()
        for key, value in all_data.items():
            if isinstance(value, dict) and value.get("id") == tmdbid:
                self.delete(key)
    
    def delete_unknown(self) -> None:
        """
        删除未识别的缓存
        """
        all_data = self.get_all()
        for key, value in all_data.items():
            if isinstance(value, dict) and value.get("id") == 0:
                self.delete(key)


class DoubanCacheManager(CacheManager):
    """
    豆瓣缓存管理器
    """
    
    def __init__(self):
        super().__init__("douban_cache", region="douban")
    
    def get_by_meta(self, meta) -> Dict[str, Any]:
        """
        根据元数据获取缓存
        
        :param meta: 元数据对象
        :return: 缓存数据
        """
        key = f"[{meta.type.value if meta.type else '未知'}]{meta.doubanid or meta.name}-{meta.year}-{meta.begin_season}"
        return self.get(key, {})
    
    def set_by_meta(self, meta, info: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """
        根据元数据设置缓存
        
        :param meta: 元数据对象
        :param info: 缓存数据
        :param ttl: 过期时间（秒）
        """
        key = f"[{meta.type.value if meta.type else '未知'}]{meta.doubanid or meta.name}-{meta.year}-{meta.begin_season}"
        self.set(key, info, ttl)
    
    def delete_by_doubanid(self, doubanid: str) -> None:
        """
        根据豆瓣ID删除缓存
        
        :param doubanid: 豆瓣ID
        """
        all_data = self.get_all()
        for key, value in all_data.items():
            if isinstance(value, dict) and value.get("id") == doubanid:
                self.delete(key)
    
    def delete_unknown(self) -> None:
        """
        删除未识别的缓存
        """
        all_data = self.get_all()
        for key, value in all_data.items():
            if isinstance(value, dict) and value.get("id") == "0":
                self.delete(key)


class TorrentsCacheManager(CacheManager):
    """
    种子缓存管理器
    """
    
    def __init__(self, cache_type: str = "spider"):
        """
        初始化种子缓存管理器
        
        :param cache_type: 缓存类型，spider或rss
        """
        cache_name = f"torrents_{cache_type}_cache"
        super().__init__(cache_name, region=f"torrents_{cache_type}")
        self.cache_type = cache_type
    
    def get_torrents(self) -> Dict[str, Any]:
        """
        获取种子缓存数据
        
        :return: 种子缓存数据
        """
        return self.get("torrents", {})
    
    def set_torrents(self, torrents: Dict[str, Any]) -> None:
        """
        设置种子缓存数据
        
        :param torrents: 种子数据
        """
        self.set("torrents", torrents)
    
    def clear_torrents(self) -> None:
        """
        清空种子缓存
        """
        self.clear()


# 全局缓存管理器实例
tmdb_cache_manager = TmdbCacheManager()
douban_cache_manager = DoubanCacheManager()
torrents_spider_cache_manager = TorrentsCacheManager("spider")
torrents_rss_cache_manager = TorrentsCacheManager("rss")