from app.modules._base import _StorageModuleBase
from app.modules.localstorage.local import LocalStorage


class LocalStorageModule(_StorageModuleBase):
    """
    本地存储模块
    """

    storage_class = LocalStorage

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "本地存储"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。"""
        return 4
