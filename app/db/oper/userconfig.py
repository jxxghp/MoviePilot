from typing import Any, Union, Dict, Optional, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.userconfig import UserConfig
from app.schemas.types import UserConfigKey
from app.foundation.singleton import Singleton
from app.db.decorators import run_legacy_sync_query


class UserConfigOper(DbOper, metaclass=Singleton):
    """
    用户配置管理
    """
    def __init__(self):
        """
        加载配置到内存
        """
        super().__init__()
        self.__USERCONF = {}
        for item in self._list_configs():
            self.__set_config_cache(username=item.username, key=item.key, value=item.value)

    def _with_sync_session(self, operation):
        """在显式会话或兼容查询会话中执行只读操作。"""
        if isinstance(self._db, Session):
            return operation(self._db)
        return run_legacy_sync_query(operation)

    def _list_configs(self) -> List[UserConfig]:
        """读取全部用户配置，避免把 None 会话传入已显式化的 Model。"""
        return self._with_sync_session(
            lambda session: list(session.execute(select(UserConfig)).scalars().all())
        )

    def _get_by_key(self, username: str, key: str) -> Optional[UserConfig]:
        """按用户名和键读取配置，复用调用方事务或一次性兼容会话。"""
        return self._with_sync_session(
            lambda session: UserConfig.get_by_key(
                db=session, username=username, key=key
            )
        )

    def set(self, username: str, key: Union[str, UserConfigKey], value: Any):
        """
        设置用户配置
        """
        if isinstance(key, UserConfigKey):
            key = key.value
        # 更新内存
        self.__set_config_cache(username=username, key=key, value=value)
        # 写入数据库
        conf = self._get_by_key(username=username, key=key)
        if conf:
            if value:
                self._stage_update(conf, {"value": value})
            else:
                self._stage_delete(UserConfig, conf.id)
        else:
            conf = UserConfig(username=username, key=key, value=value)
            self._stage_create(conf)

    def get(self, username: str, key: Optional[Union[str, UserConfigKey]] = None) -> Any:
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
