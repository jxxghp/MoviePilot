from types import SimpleNamespace

from app.core.config import settings
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.schemas.types import MediaType


class _MemoryCacheStub:
    """TMDB 识别缓存测试用的最小内存后端。"""

    def __init__(self, data: dict = None):
        """使用给定字典初始化测试缓存。"""
        self.data = data if data is not None else {}
        self.ttls = {}

    def get(self, key: str):
        """读取指定缓存条目。"""
        return self.data.get(key)

    def set(self, key: str, value, ttl=None):
        """写入缓存条目并记录使用的 TTL。"""
        self.data[key] = value
        self.ttls[key] = ttl

    def delete(self, key: str):
        """删除指定缓存条目。"""
        self.data.pop(key, None)

    def items(self):
        """返回全部缓存条目。"""
        return self.data.items()

    @staticmethod
    def is_redis() -> bool:
        """测试后端不是 Redis。"""
        return False


def _build_cache(data: dict = None) -> TmdbCache:
    """构造绕过单例初始化的 TMDB 缓存实例。"""
    cache = object.__new__(TmdbCache)
    cache._cache = _MemoryCacheStub(data)
    cache.ttl = 43200
    cache._expires_at = {}
    cache._dirty = False
    cache.save = lambda force=False: None
    return cache


def _build_meta(mtype: MediaType, begin_season=1) -> SimpleNamespace:
    """构造识别缓存所需的最小元数据。"""
    return SimpleNamespace(type=mtype, tmdbid=329809, year="2022",
                           begin_season=begin_season, name="死神")


def _key(type_name: str, begin_season=1) -> str:
    """按缓存键格式构造测试用键。"""
    return f"[{type_name}][{settings.TMDB_LOCALE}]329809-2022-{begin_season}"


def test_get_discards_movie_value_cached_under_tv_key():
    """电视剧元数据命中电影缓存值属于脏条目，应丢弃并回源。"""
    meta = _build_meta(MediaType.TV)
    key = _key("电视剧")
    cache = _build_cache({key: {"id": 329809, "type": MediaType.MOVIE,
                                "title": "白鼬", "year": "2015"}})

    assert cache.get(meta) == {}
    assert cache._cache.get(key) is None


def test_get_discards_movie_value_stored_as_plain_string():
    """缓存值经序列化后类型退化为字符串时，同样要识别出冲突。"""
    meta = _build_meta(MediaType.TV)
    key = _key("电视剧")
    cache = _build_cache({key: {"id": 329809, "type": "电影", "title": "白鼬"}})

    assert cache.get(meta) == {}


def test_get_keeps_tv_value_cached_under_movie_key():
    """电影元数据命中电视剧缓存值是名称识别的正常纠正结果，必须保留。"""
    meta = _build_meta(MediaType.MOVIE, begin_season=None)
    key = _key("电影", begin_season=None)
    cache = _build_cache({key: {"id": 1, "type": MediaType.TV, "title": "某剧"}})

    assert cache.get(meta)["type"] == MediaType.TV


def test_get_keeps_negative_cache_entry():
    """负缓存不带类型信息，不应被类型校验误删。"""
    meta = _build_meta(MediaType.TV)
    cache = _build_cache({_key("电视剧"): {"id": 0}})

    assert cache.get(meta) == {"id": 0}


def test_update_refuses_to_cache_movie_result_for_tv_meta():
    """电视剧元数据得到电影识别结果时不得写入缓存，避免固化脏条目。"""
    meta = _build_meta(MediaType.TV)
    cache = _build_cache()

    cache.update(meta, {"id": 329809, "media_type": MediaType.MOVIE,
                        "title": "白鼬", "release_date": "2015-01-01"})

    assert cache._cache.data == {}


def test_update_caches_tv_result_for_movie_meta():
    """电影元数据得到电视剧结果是正常纠正，应照常写入缓存。"""
    meta = _build_meta(MediaType.MOVIE, begin_season=None)
    cache = _build_cache()

    cache.update(meta, {"id": 1, "media_type": MediaType.TV,
                        "name": "某剧", "first_air_date": "2022-01-01"})

    stored = cache._cache.get(_key("电影", begin_season=None))
    assert stored["type"] == MediaType.TV
    assert stored["title"] == "某剧"
