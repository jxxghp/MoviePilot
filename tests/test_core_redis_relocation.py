import inspect
from unittest import TestCase


class CoreRedisRelocationTest(TestCase):
    """RedisHelper/AsyncRedisHelper 迁移至 app.core.redis：核心可用、helper 垫片兼容
    （含 p115strmhelper 等插件的历史导入路径）、core/cache 不再反向依赖 helper.redis。"""

    def test_helper_shim_reexports_same_classes(self):
        """app.helper.redis 作为兼容垫片应 re-export 与 app.core.redis 同一的类。"""
        from app.core.redis import RedisHelper as CoreR, AsyncRedisHelper as CoreAR
        from app.helper.redis import RedisHelper as HelperR, AsyncRedisHelper as HelperAR
        self.assertIs(CoreR, HelperR)
        self.assertIs(CoreAR, HelperAR)

    def test_helper_shim_reexports_serialize_helpers(self):
        """模块级公共函数 serialize/deserialize 也应通过垫片保持可用。"""
        from app.core.redis import serialize as core_ser, deserialize as core_de
        from app.helper.redis import serialize as helper_ser, deserialize as helper_de
        self.assertIs(core_ser, helper_ser)
        self.assertIs(core_de, helper_de)

    def test_core_cache_no_longer_imports_helper_redis(self):
        """core/cache 不应再从 helper 反向导入 Redis 封装。"""
        import app.core.cache as cache_mod
        self.assertNotIn("app.helper.redis", inspect.getsource(cache_mod))

    def test_core_redis_has_no_helper_dependency(self):
        """新的 core.redis 自身不应依赖 helper 层。"""
        import app.core.redis as redis_mod
        self.assertNotIn("app.helper", inspect.getsource(redis_mod))
