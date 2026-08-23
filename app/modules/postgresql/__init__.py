from typing import Tuple, Union

from app.application.database import get_database_governance
from app.modules import _ModuleBase
from app.runtime.settings import RuntimeSettingsCompat
from app.schemas.types import ModuleType, OtherModulesType

settings = RuntimeSettingsCompat()


class PostgreSQLModule(_ModuleBase):
    """
    PostgreSQL 数据库模块
    """

    def init_module(self) -> None:
        """PostgreSQL 连接由数据库 adapter 管理，无需模块级初始化。"""
        pass

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "PostgreSQL"

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
        return OtherModulesType.PostgreSQL

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """数据库类型由部署配置决定，不声明独立模块开关。"""
        pass

    def stop(self) -> None:
        """连接池由数据库生命周期释放，本模块无独立资源。"""
        pass

    def test(self):
        """
        测试模块连接性
        """
        if settings.DB_TYPE != "postgresql":
            return None
        error = get_database_governance().test()
        if error:
            return False, f"PostgreSQL连接失败：{error}"
        return True, ""
