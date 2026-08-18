from app.modules._base import _StorageModuleBase
from app.modules.alist.alist import Alist


class AlistModule(_StorageModuleBase):
    """
    Alist 存储模块
    """

    storage_class = Alist

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "Alist"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。"""
        return 2
