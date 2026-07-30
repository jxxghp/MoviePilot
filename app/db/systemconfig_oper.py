import asyncio
import copy
import threading
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
        self._rlock = threading.RLock()
        self._alock = asyncio.Lock()
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
        with self._rlock:
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

    async def async_set(self, key: Union[str, SystemConfigKey], value: Any) -> Optional[bool]:
        """
        异步设置系统设置
        :param key: 配置键
        :param value: 配置值
        :return: 是否设置成功（True 成功/False 失败/None 无需更新）
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        async with self._alock:
            conf = await SystemConfig.async_get_by_key(self._db, key)
            # 确定是否需要更新数据库
            needs_db_update = False
            if conf:
                if conf.value != value:
                    needs_db_update = True
            else:  # 记录不存在，总是需要创建/更新
                needs_db_update = True
            if not needs_db_update:
                # 即使数据库值相同，也要确保缓存同步
                with self._rlock:
                    self.__SYSTEMCONF[key] = copy.deepcopy(value)
                return None
            # 执行数据库更新
            if conf:
                if value:
                    await conf.async_update(self._db, {"value": value})
                else:
                    await conf.async_delete(self._db, conf.id)
            else:
                conf = SystemConfig(key=key, value=value)
                await conf.async_create(self._db)
            # 数据库更新成功后，再更新缓存
            with self._rlock:
                self.__SYSTEMCONF[key] = copy.deepcopy(value)
            return True

    def get(self, key: Union[str, SystemConfigKey] = None) -> Any:
        """
        获取系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        if not key:
            return self.all()
        with self._rlock:
            # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
            return copy.deepcopy(self.__SYSTEMCONF.get(key))

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """
        原子递增整数系统设置

        :param key: 配置键
        :param step: 递增步长
        :return: 递增后的整数值
        """
        with self._rlock:
            value = int(self.get(key) or 0) + step
            self.set(key, value)
            return value

    def all(self):
        """
        获取所有系统设置
        """
        with self._rlock:
            # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
            return copy.deepcopy(self.__SYSTEMCONF)

    def delete(self, key: Union[str, SystemConfigKey]) -> bool:
        """
        删除系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        with self._rlock:
            # 更新内存
            self.__SYSTEMCONF.pop(key, None)
            # 写入数据库
            conf = SystemConfig.get_by_key(self._db, key)
            if conf:
                conf.delete(self._db, conf.id)
            return True
