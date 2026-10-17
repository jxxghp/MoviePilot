"""
存储服务配置的读取、更新与按类型查询。
"""

from typing import List, Optional, TypedDict

from app.application.configuration import get_configured_system_config
from app.schemas.system import StorageConf as _SchemaStorageConf
from app.schemas.types import SystemConfigKey


class _StorageConfigRecord(TypedDict, total=False):
    """
    StorageConf 持久化字典的可选字段，保留模型默认值和校验。

    :ivar type: 存储类型标识；键可缺失，缺失或 None 的校验由 StorageConf 执行
    :ivar name: 存储实例的展示名称；键可缺失，None 表示未提供名称
    :ivar config: 对应存储类型的连接配置；键可缺失，None 表示未提供配置
    """

    type: str | None
    name: str | None
    config: dict[str, object] | None


class StorageHelper:
    """
    存储帮助类
    """

    @staticmethod
    def get_storagies() -> List[_SchemaStorageConf]:
        """
        获取所有存储设置
        """
        storage_confs: List[_StorageConfigRecord] | None = get_configured_system_config().get(SystemConfigKey.Storages)
        if not storage_confs:
            return []
        return [_SchemaStorageConf(**s) for s in storage_confs]

    def get_storage(self, storage: str) -> Optional[_SchemaStorageConf]:
        """
        获取指定存储配置
        """
        storagies = self.get_storagies()
        for s in storagies:
            if s.type == storage:
                return s
        return None

    def set_storage(self, storage: str, conf: dict[str, object]) -> None:
        """
        设置存储配置
        """
        storagies = self.get_storagies()
        if not storagies:
            storagies = [
                _SchemaStorageConf(
                    type=storage,
                    config=conf
                )
            ]
        else:
            for s in storagies:
                if s.type == storage:
                    s.config = conf
                    break
        get_configured_system_config().set(SystemConfigKey.Storages, [s.model_dump() for s in storagies])

    def add_storage(self, storage: str, name: str, conf: dict[str, object]) -> None:
        """
        添加存储配置
        """
        storagies = self.get_storagies()
        if not storagies:
            storagies = [
                _SchemaStorageConf(
                    type=storage,
                    name=name,
                    config=conf
                )
            ]
        else:
            storagies.append(_SchemaStorageConf(
                type=storage,
                name=name,
                config=conf
            ))
        get_configured_system_config().set(SystemConfigKey.Storages, [s.model_dump() for s in storagies])

    def reset_storage(self, storage: str) -> None:
        """
        重置存储配置
        """
        storagies = self.get_storagies()
        for s in storagies:
            if s.type == storage:
                s.config = {}
                break
        get_configured_system_config().set(SystemConfigKey.Storages, [s.model_dump() for s in storagies])
