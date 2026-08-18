from app.modules._base import _StorageModuleBase
from app.modules.alistgo.alistgo import AlistGo


class AlistGoModule(_StorageModuleBase):
    """
    AListGo 存储模块
    """

    storage_class = AlistGo

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "AListGo"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。"""
        return 3
