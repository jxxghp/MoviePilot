"""
AListGo 存储类型及 OpenList 兼容能力复用。
"""

from app.modules.filemanager.storages.alist import Alist
from app.schemas.types import StorageSchema


class AlistGo(Alist):
    """
    复用 OpenList 兼容接口，为 AListGo 绑定独立存储类型。

    API 文档：https://docs.alistgo.com/
    """

    schema = StorageSchema.AlistGo
