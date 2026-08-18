from app.modules._base import _StorageModuleBase
from app.modules.u115.u115 import U115Pan


class U115Module(_StorageModuleBase):
    """
    115 网盘存储模块
    """

    storage_class = U115Pan

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "115网盘"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。"""
        return 7
