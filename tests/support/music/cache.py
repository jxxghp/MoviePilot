"""
音乐识别缓存替身及模块装配；不注册测试用例。
"""

from app.modules.musicbrainz import MusicBrainzModule
from app.modules.musicbrainz.cache import MusicBrainzCache


class _MemoryCacheStub:
    """
    提供音乐缓存管理测试所需的最小内存后端。
    """

    def __init__(self, data: dict):
        """
        使用给定字典初始化测试缓存。
        """
        self.data = data

    @staticmethod
    def is_redis() -> bool:
        """
        测试替身固定使用非 Redis 后端。
        """
        return False

    def items(self):
        """
        返回全部缓存条目。
        """
        return self.data.items()

    def get(self, key: str):
        """
        读取指定缓存条目。
        """
        return self.data.get(key)

    def delete(self, key: str):
        """
        删除指定缓存条目。
        """
        self.data.pop(key, None)

    def set(self, key: str, value, ttl=None):
        """
        写入指定缓存条目。
        """
        self.data[key] = value

    def clear(self):
        """
        清空全部缓存条目。
        """
        self.data.clear()


class _TTLCacheStub(_MemoryCacheStub):
    """
    记录每条数据恢复时剩余 TTL 的内存缓存替身。
    """

    def __init__(self):
        """
        初始化空缓存和 TTL 记录。
        """
        super().__init__({})
        self.ttls = {}

    def set(self, key: str, value, ttl=None):
        """
        写入缓存并记录本次设置的 TTL。
        """
        super().set(key, value, ttl=ttl)
        self.ttls[key] = ttl


def _build_music_cache(data: dict) -> MusicBrainzCache:
    """
    构造绕过单例初始化的音乐识别缓存测试实例。
    """
    cache = object.__new__(MusicBrainzCache)
    cache.maxsize = 256
    cache.ttl = 3600
    cache.region = "__musicbrainz_cache__"
    cache._cache = _MemoryCacheStub(data)
    cache._expires_at = {key: float("inf") for key in data}
    cache._dirty = False
    cache._file_cache = None
    cache.save = lambda force=False: None
    return cache


def _build_module_with_cache(cache: MusicBrainzCache) -> MusicBrainzModule:
    """
    构造挂载测试缓存的 MusicBrainz 模块实例。
    """
    module = MusicBrainzModule()
    module.cache = cache
    return module
