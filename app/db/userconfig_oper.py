import json
from typing import Any, Union, Dict, Optional

from app.db import DbOper
from app.db.models.userconfig import UserConfig
from app.schemas.types import UserConfigKey
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton


class UserConfigOper(DbOper, metaclass=Singleton):
    # 配置缓存
    __USERCONF: Dict[str, Dict[str, Any]] = {}

    def __init__(self):
        """
        加载配置到内存
        """
        super().__init__()
        for item in UserConfig.list(self._db):
            value = json.loads(item.value) if ObjectUtils.is_obj(item.value) else item.value
            self.__set_config_cache(username=item.username, key=item.key, value=value)

    def set(self, username: str, key: Union[str, UserConfigKey], value: Any):
        """
        设置用户配置
        """
        if isinstance(key, UserConfigKey):
            key = key.value
        # 更新内存
        self.__set_config_cache(username=username, key=key, value=value)
        # 写入数据库
        if ObjectUtils.is_obj(value):
            value = json.dumps(value)
        elif value is None:
            value = ''
        conf = UserConfig.get_by_key(db=self._db, username=username, key=key)
        if conf:
            if value:
                conf.update(self._db, {"value": value})
            else:
                conf.delete(self._db, conf.id)
        else:
            conf = UserConfig(username=username, key=key, value=value)
            conf.create(self._db)

    def get(self, username: str, key: Union[str, UserConfigKey] = None) -> Any:
        """
        获取用户配置
        """
        if not username:
            return self.__USERCONF
        if isinstance(key, UserConfigKey):
            key = key.value
        if not key:
            return self.__get_config_caches(username=username)
        return self.__get_config_cache(username=username, key=key)

    def __del__(self):
        if self._db:
            self._db.close()

    def __set_config_cache(self, username: str, key: str, value: Any):
        """
        设置配置缓存
        """
        if not username or not key:
            return
        cache = self.__USERCONF
        if not cache:
            cache = {}
        user_cache = cache.get(username)
        if not user_cache:
            user_cache = {}
            cache[username] = user_cache
        user_cache[key] = value
        self.__USERCONF = cache

    def __get_config_caches(self, username: str) -> Optional[Dict[str, Any]]:
        """
        获取配置缓存
        """
        if not username or not self.__USERCONF:
            return None
        return self.__USERCONF.get(username)

    def __get_config_cache(self, username: str, key: str) -> Any:
        """
        获取配置缓存
        """
        if not username or not key or not self.__USERCONF:
            return None
        user_cache = self.__get_config_caches(username)
        if not user_cache:
            return None
        return user_cache.get(key)
