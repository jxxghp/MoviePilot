from typing import Any, Union

from app.core.config import ConfigChangeEvent, config_notifier, ConfigChangeType
from app.db import DbOper
from app.db.models.systemconfig import SystemConfig
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class SystemConfigOper(DbOper, metaclass=Singleton):
    # 配置对象
    __SYSTEMCONF: dict = {}

    def __init__(self):
        """
        加载配置到内存
        """
        super().__init__()
        for item in SystemConfig.list(self._db):
            self.__SYSTEMCONF[item.key] = item.value

    def set(self, key: Union[str, SystemConfigKey], value: Any):
        """
        设置系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        # 旧值
        old_value = self.__SYSTEMCONF.get(key)
        # 更新内存
        self.__SYSTEMCONF[key] = value
        conf = SystemConfig.get_by_key(self._db, key)
        if conf:
            if value:
                conf.update(self._db, {"value": value})
                # 发送配置变更通知
                if old_value != value:
                    event = ConfigChangeEvent(key, old_value=old_value, new_value=value,
                                              change_type=ConfigChangeType.UPDATE)
                    config_notifier.notify(event)
            else:
                conf.delete(self._db, conf.id)
                # 发送配置删除通知
                event = ConfigChangeEvent(key, old_value=old_value, new_value=None,
                                          change_type=ConfigChangeType.DELETE)
                config_notifier.notify(event)
        else:
            conf = SystemConfig(key=key, value=value)
            conf.create(self._db)
            # 发送配置变更通知
            event = ConfigChangeEvent(key, old_value=None, new_value=value,
                                      change_type=ConfigChangeType.ADD)
            config_notifier.notify(event)

    def get(self, key: Union[str, SystemConfigKey] = None) -> Any:
        """
        获取系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        if not key:
            return self.__SYSTEMCONF
        return self.__SYSTEMCONF.get(key)

    def all(self):
        """
        获取所有系统设置
        """
        return self.__SYSTEMCONF or {}

    def delete(self, key: Union[str, SystemConfigKey]):
        """
        删除系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        # 更新内存
        old_value = self.__SYSTEMCONF.pop(key, None)
        # 写入数据库
        conf = SystemConfig.get_by_key(self._db, key)
        if conf:
            conf.delete(self._db, conf.id)
            # 发送配置变更通知
            event = ConfigChangeEvent(key, old_value=old_value, new_value=None,
                                      change_type=ConfigChangeType.ADD)
            config_notifier.notify(event)
        return True

    def __del__(self):
        if self._db:
            self._db.close()
