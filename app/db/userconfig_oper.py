import json
from typing import Any, Union, Dict

from app.db import DbOper
from app.db.models.userconfig import UserConfig
from app.schemas.types import UserConfigKey
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton


class UserConfigOper(DbOper, metaclass=Singleton):
    # 配置缓存
    __USERCONF: Dict[int, Dict[str, Any]] = {}

    def __init__(self):
        """
        加载配置到内存
        """
        super().__init__()
        for item in UserConfig.list(self._db):
            self.__set_config_cache(user_id=item.user_id, key=item.key, value=item.value)

    def set(self, user_id: int, key: Union[str, UserConfigKey], value: Any):
        """
        设置用户配置
        """
        if isinstance(key, UserConfigKey):
            key = key.value
        # 更新内存
        self.__set_config_cache(user_id=user_id, key=key, value=value)
        # 写入数据库
        if ObjectUtils.is_obj(value):
            value = json.dumps(value)
        elif value is None:
            value = ''
        conf = UserConfig.get_by_key(self._db, user_id, key)
        if conf:
            if value:
                conf.update(self._db, {"value": value})
            else:
                conf.delete(self._db, conf.id)
        else:
            conf = UserConfig(user_id=user_id, key=key, value=value)
            conf.create(self._db)

    def get(self, user_id: int, key: Union[str, UserConfigKey] = None) -> Any:
        """
        获取用户配置
        """
        if not user_id:
            return self.__USERCONF
        if isinstance(key, UserConfigKey):
            key = key.value
        if not key:
            return self.__get_config_caches(user_id=user_id)
        return self.__get_config_cache(user_id=user_id, key=key)

    def __del__(self):
        if self._db:
            self._db.close()

    def __set_config_cache(self, user_id: int, key: str, value: Any):
        """
        设置配置缓存
        """
        if not user_id or not key:
            return
        cache = self.__USERCONF
        if not cache:
            cache = {}
        user_cache = cache.get(user_id)
        if not user_cache:
            user_cache = {}
            cache[user_id] = user_cache
        if ObjectUtils.is_obj(value):
            user_cache[key] = json.loads(value)
        else:
            user_cache[key] = value
        self.__USERCONF = cache
    
    def __get_config_caches(self, user_id: int) -> Dict[str, Any]:
        """
        获取配置缓存
        """
        if not user_id:
            return None
        if not self.__USERCONF:
            return None
        return self.__USERCONF.get(user_id)

    def __get_config_cache(self, user_id: int, key: str) -> Any:
        """
        获取配置缓存
        """
        if not user_id or not key:
            return None
        if not self.__USERCONF:
            return None
        user_cache = self.__USERCONF.get(user_id)
        if not user_cache:
            return None
        return user_cache.get(key)
