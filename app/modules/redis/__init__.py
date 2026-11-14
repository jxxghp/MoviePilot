"""
Redis 缓存的模块声明与已注册后端的连通性检查。
"""

from typing import Optional, Tuple

from app.modules import _ModuleBase
from app.runtime.cache import probe_redis_cache
from app.runtime.settings import get_runtime_setting
from app.schemas.types import ModuleType, OtherModulesType


class RedisModule(_ModuleBase):
    """
    Redis 数据库模块。
    """

    def init_module(self) -> None:
        """
        Redis 客户端由缓存适配器 惰性管理，无需模块级初始化。
        """
        pass

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Redis缓存"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型。
        """
        return OtherModulesType.Redis

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 0

    def init_setting(self) -> None:
        """
        缓存后端由部署配置决定，不声明独立模块开关。
        """
        pass

    def stop(self) -> None:
        """
        缓存适配器 负责连接释放，本模块无独立资源。
        """
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查启动时已接入的 Redis 缓存。

        不在检查过程中创建连接；配置刚切换但尚未注册后端时提示重启。

        :return: 连通结果与说明；未选择 Redis 缓存时返回 None
        """
        if get_runtime_setting('CACHE_BACKEND_TYPE') != "redis":
            return None
        # Redis 由缓存组合根在启动时按配置接入；缓存设置需重启生效，未接入时不代为建连。
        connected = probe_redis_cache()
        if connected is None:
            return False, "缓存类型已改为Redis，重启后生效"
        if connected:
            return True, ""
        return False, "Redis连接失败，请检查配置"
