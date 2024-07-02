from typing import List

from app import schemas
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class StorageHelper:
    """
    存储帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_storagies(self) -> List[schemas.StorageConf]:
        """
        获取所有存储设置
        """
        storage_confs: List[dict] = self.systemconfig.get(SystemConfigKey.Storages)
        if not storage_confs:
            return []
        return [schemas.StorageConf(**s) for s in storage_confs]
