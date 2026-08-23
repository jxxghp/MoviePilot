import copy
import threading
from typing import Any, Union, Dict, Optional

from app.db.base import DbOper
from app.db.models.userconfig import UserConfig
from app.schemas.types import UserConfigKey
from app.foundation.singleton import Singleton


class UserConfigOper(DbOper, metaclass=Singleton):
    """
    用户配置管理
    """
    def __init__(self):
        """初始化空快照，数据库加载由启动组合根显式执行。"""
        super().__init__()
        self.__USERCONF = {}
        self._snapshot_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._loaded = False

    def load_snapshot(self) -> None:
        """从数据库加载完整用户配置，并一次性发布新的内存快照。"""
        with self._write_lock:
            snapshot: dict[str, dict[str, Any]] = {}
            for item in UserConfig.list(self._db):
                if item.username and item.key:
                    snapshot.setdefault(item.username, {})[item.key] = copy.deepcopy(
                        item.value
                    )
            with self._snapshot_lock:
                self.__USERCONF = snapshot
                self._loaded = True

    def _require_loaded(self) -> None:
        """阻止消费者读取尚未完成启动加载的半成品快照。"""
        if not self._loaded:
            raise RuntimeError("用户配置快照尚未加载")

    def set(self, username: str, key: Union[str, UserConfigKey], value: Any):
        """
        设置用户配置
        """
        if isinstance(key, UserConfigKey):
            key = key.value
        self._require_loaded()
        with self._write_lock:

            def write(db):
                """在当前事务中按用户配置的假值规则写入记录。"""
                conf = UserConfig.get_by_key(db=db, username=username, key=key)
                if conf:
                    if value:
                        conf.value = copy.deepcopy(value)
                    else:
                        db.delete(conf)
                else:
                    db.add(
                        UserConfig(
                            username=username,
                            key=key,
                            value=copy.deepcopy(value),
                        )
                    )

            self._execute_sync_write(write)
            # 既有运行时语义会保留刚写入的假值，即使其数据库记录被删除。
            self.__set_config_cache(username=username, key=key, value=value)

    def get(self, username: str, key: Optional[Union[str, UserConfigKey]] = None) -> Any:
        """
        获取用户配置
        """
        with self._snapshot_lock:
            self._require_loaded()
            if not username:
                return copy.deepcopy(self.__USERCONF)
            if isinstance(key, UserConfigKey):
                key = key.value
            if not key:
                return copy.deepcopy(self.__get_config_caches(username=username))
            return copy.deepcopy(self.__get_config_cache(username=username, key=key))

    def __set_config_cache(self, username: str, key: str, value: Any):
        """
        设置配置缓存
        """
        if not username or not key:
            return
        with self._snapshot_lock:
            user_cache = self.__USERCONF.setdefault(username, {})
            user_cache[key] = copy.deepcopy(value)

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
