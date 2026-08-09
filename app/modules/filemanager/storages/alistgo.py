from app.modules.filemanager.storages.alist import Alist
from app.schemas.types import StorageSchema


class AlistGo(Alist):
    """
    AListGo 相关操作

    API 文档：https://docs.alistgo.com/
    """

    schema = StorageSchema.AlistGo
