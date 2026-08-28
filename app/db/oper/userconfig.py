import copy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional, Union

from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.userconfig import UserConfig
from app.foundation.singleton import Singleton
from app.schemas.common import JsonData
from app.schemas.types import UserConfigKey


class UserConfigOper(DbOper, metaclass=Singleton):
    """
    用户配置管理
    """
    def __init__(self) -> None:
        """初始化空快照，数据库加载由启动组合根显式执行。"""
        super().__init__()
        self.__USERCONF: dict[str, dict[str, JsonData]] = {}
        self._snapshot_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._loaded = False

    def load_snapshot(self, db: Optional[Session] = None) -> None:
        """从显式会话或 Oper 事务边界加载用户配置并发布内存快照。"""
        with self._write_lock:
            snapshot: dict[str, dict[str, JsonData]] = {}
            items = UserConfig.list(db) if db is not None else self._execute_sync_query(
                UserConfig.list
            )
            for item in items:
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

    @contextmanager
    def write_scope(self) -> Iterator[None]:
        """串行化数据库提交与对应快照发布，避免并发写入乱序。"""
        self._require_loaded()
        with self._write_lock:
            yield

    def stage_set(
        self,
        db: Session,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> bool:
        """在调用方 Session 中暂存写入，返回提交后是否应移除缓存项。"""
        if isinstance(key, UserConfigKey):
            key = key.value
        conf = UserConfig.get_by_key(db=db, username=username, key=key)
        if conf:
            if value:
                conf.value = copy.deepcopy(value)
                return False
            db.delete(conf)
            return True
        db.add(
            UserConfig(
                username=username,
                key=key,
                value=copy.deepcopy(value),
            )
        )
        return False

    def publish(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
        *,
        deleted: bool,
    ) -> None:
        """仅在数据库提交成功后原子发布对应配置快照。"""
        if isinstance(key, UserConfigKey):
            key = key.value
        if not username or not key:
            return
        with self._snapshot_lock:
            if deleted:
                user_cache = self.__USERCONF.get(username)
                if user_cache is None:
                    return
                user_cache.pop(key, None)
                if not user_cache:
                    self.__USERCONF.pop(username, None)
                return
            self.__USERCONF.setdefault(username, {})[key] = copy.deepcopy(value)

    def publish_rename(self, previous_name: str, current_name: str) -> None:
        """原子迁移已提交改名对应的配置快照，并清理目标孤儿配置。"""
        if not previous_name or not current_name or previous_name == current_name:
            return
        with self._snapshot_lock:
            self._require_loaded()
            values = self.__USERCONF.pop(previous_name, None)
            self.__USERCONF.pop(current_name, None)
            if values:
                self.__USERCONF[current_name] = copy.deepcopy(values)

    def publish_delete(self, username: str) -> None:
        """原子移除已提交用户删除对应的配置快照。"""
        if not username:
            return
        with self._snapshot_lock:
            self._require_loaded()
            self.__USERCONF.pop(username, None)

    def set(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> None:
        """
        通过兼容事务入口设置用户配置。

        新宿主调用应使用 ``TransactionalUserConfigurationRepository``；此方法保留给
        旧插件 ABI，并与规范适配器共享同一暂存、提交后发布及失败恢复语义。
        """
        with self.write_scope():
            deleted = self._execute_sync_write(
                lambda db: self.stage_set(db, username, key, value)
            )
            try:
                self.publish(username, key, value, deleted=deleted)
            except Exception:
                self.load_snapshot()
                raise

    def get(
        self,
        username: Optional[str],
        key: Optional[Union[str, UserConfigKey]] = None,
    ) -> JsonData:
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
                return copy.deepcopy(self.__USERCONF.get(username))
            user_cache = self.__USERCONF.get(username)
            return copy.deepcopy(user_cache.get(key) if user_cache else None)
