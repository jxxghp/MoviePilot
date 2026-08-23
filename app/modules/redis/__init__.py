from typing import Tuple, Union

from app.adapters.cache.redis import RedisHelper
from app.modules import _ModuleBase
from app.runtime.settings import RuntimeSettingsCompat
from app.schemas.types import ModuleType, OtherModulesType

settings = RuntimeSettingsCompat()


class RedisModule(_ModuleBase):
    """
    Redis 数据库模块
    """

    def init_module(self) -> None:
        """Redis 客户端由缓存 adapter 惰性管理，无需模块级初始化。"""
        pass

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "Redis缓存"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Redis

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """缓存后端由部署配置决定，不声明独立模块开关。"""
        pass

    def stop(self) -> None:
        """缓存 adapter 负责连接释放，本模块无独立资源。"""
        pass

    def test(self):
        """
        测试模块连接性
        """
        if settings.CACHE_BACKEND_TYPE != "redis":
            return None
        if RedisHelper().test():
            return True, ""
        return False, "Redis连接失败，请检查配置"
