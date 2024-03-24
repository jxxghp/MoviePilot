import json
from typing import Any, Union

from app.db import DbOper
from app.db.models.systemconfig import SystemConfig
from app.schemas.types import SystemConfigKey
from app.utils.object import ObjectUtils
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
            if ObjectUtils.is_obj(item.value):
                self.__SYSTEMCONF[item.key] = json.loads(item.value)
            else:
                self.__SYSTEMCONF[item.key] = item.value

    def set(self, key: Union[str, SystemConfigKey], value: Any):
        """
        设置系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        # 更新内存
        self.__SYSTEMCONF[key] = value
        # 写入数据库
        if ObjectUtils.is_obj(value):
            value = json.dumps(value)
        elif value is None:
            value = ''
        conf = SystemConfig.get_by_key(self._db, key)
        if conf:
            if value:
                conf.update(self._db, {"value": value})
            else:
                conf.delete(self._db, conf.id)
        else:
            conf = SystemConfig(key=key, value=value)
            conf.create(self._db)

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
        self.__SYSTEMCONF.pop(key, None)
        # 写入数据库
        conf = SystemConfig.get_by_key(self._db, key)
        if conf:
            conf.delete(self._db, conf.id)
        return True

    def __del__(self):
        if self._db:
            self._db.close()
