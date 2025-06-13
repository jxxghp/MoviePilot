import copy
from typing import Any, Optional, Union

from app.db import DbOper
from app.db.models.systemconfig import SystemConfig
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class SystemConfigOper(DbOper, metaclass=Singleton):
    """
    系统配置管理
    """
    def __init__(self):
        """
        加载配置到内存
        """
        super().__init__()
        self.__SYSTEMCONF = {}
        for item in SystemConfig.list(self._db):
            self.__SYSTEMCONF[item.key] = item.value

    def set(self, key: Union[str, SystemConfigKey], value: Any) -> Optional[bool]:
        """
        设置系统设置
        :param key: 配置键
        :param value: 配置值
        :return: 是否设置成功（True 成功/False 失败/None 无需更新）
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        # 旧值
        old_value = self.__SYSTEMCONF.get(key)
        # 更新内存(deepcopy避免内存共享)
        self.__SYSTEMCONF[key] = copy.deepcopy(value)
        conf = SystemConfig.get_by_key(self._db, key)
        if conf:
            if old_value != value:
                if value:
                    conf.update(self._db, {"value": value})
                else:
                    conf.delete(self._db, conf.id)
                return True
            return None
        else:
            conf = SystemConfig(key=key, value=value)
            conf.create(self._db)
            return True

    def get(self, key: Union[str, SystemConfigKey] = None) -> Any:
        """
        获取系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        if not key:
            return self.all()
        # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
        return copy.deepcopy(self.__SYSTEMCONF.get(key))

    def all(self):
        """
        获取所有系统设置
        """
        # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
        return copy.deepcopy(self.__SYSTEMCONF)

    def delete(self, key: Union[str, SystemConfigKey]) -> bool:
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
