from typing import Tuple, Union

from app.core.config import settings
from app.helper.redis import RedisHelper
from app.modules import _ModuleBase
from app.schemas.types import ModuleType, OtherModulesType


class RedisModule(_ModuleBase):
    """
    Redis 数据库模块
    """

    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
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
        pass

    def stop(self) -> None:
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
