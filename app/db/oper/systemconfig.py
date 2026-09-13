import copy
import threading
from collections.abc import Callable, Mapping
from typing import Any, Optional, TypeVar, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.systemconfig import SystemConfig
from app.foundation.singleton import Singleton
from app.schemas.types import SystemConfigKey

T = TypeVar("T")


class SystemConfigOper(DbOper, metaclass=Singleton):
    """
    系统配置管理
    """
    def __init__(self):
        """初始化空快照，数据库加载由启动组合根显式执行。"""
        super().__init__()
        self.__SYSTEMCONF = {}
        self._snapshot_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._loaded = False

    def load_snapshot(self, db: Optional[Session] = None) -> None:
        """从显式会话或 Oper 事务边界加载配置并发布内存快照。"""
        with self._write_lock:
            items = SystemConfig.list(db) if db is not None else self._execute_sync_query(
                SystemConfig.list
            )
            snapshot = {
                item.key: copy.deepcopy(item.value)
                for item in items
            }
            with self._snapshot_lock:
                self.__SYSTEMCONF = snapshot
                self._loaded = True

    def _require_loaded(self) -> None:
        """阻止消费者读取尚未完成启动加载的半成品快照。"""
        if not self._loaded:
            raise RuntimeError("系统配置快照尚未加载")

    def _publish_value(self, key: str, value: Any) -> None:
        """在事务成功后短暂持锁发布单项配置。"""
        with self._snapshot_lock:
            self.__SYSTEMCONF[key] = copy.deepcopy(value)

    def _publish_delete(self, key: str) -> None:
        """在事务成功后短暂持锁移除单项配置。"""
        with self._snapshot_lock:
            self.__SYSTEMCONF.pop(key, None)

    def publish_many(
        self,
        values: Mapping[SystemConfigKey, Any],
    ) -> None:
        """在外部事务提交后一次发布多项配置快照。"""
        with self._snapshot_lock:
            self._require_loaded()
            for key, value in values.items():
                self.__SYSTEMCONF[key.value] = copy.deepcopy(value)

    def set(self, key: Union[str, SystemConfigKey], value: Any) -> Optional[bool]:
        """
        设置系统设置
        :param key: 配置键
        :param value: 配置值
        :return: 是否设置成功（True 成功/False 失败/None 无需更新）
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        self._require_loaded()
        with self._write_lock:

            def write(db):
                """在当前事务中创建或更新配置记录。"""
                conf = SystemConfig.get_by_key(db, key)
                if conf:
                    if conf.value == value:
                        return None
                    # 假值同样是有效配置；删除记录会使读取端错误回落默认值。
                    conf.value = copy.deepcopy(value)
                else:
                    db.add(SystemConfig(key=key, value=copy.deepcopy(value)))
                return True

            result = self._execute_sync_write(write)
            # 数据库操作返回时事务已经提交，读取方不会看到尚未持久化的配置。
            self._publish_value(key, value)
            return result

    def update_atomically(
        self,
        key: Union[str, SystemConfigKey],
        mutation: Callable[[Session, Any], tuple[T, Any]],
    ) -> T:
        """在配置写锁内提交关联记录，并在事务成功后发布最终配置值。"""
        if isinstance(key, SystemConfigKey):
            key = key.value
        self._require_loaded()
        with self._write_lock:

            def write(db: Session) -> tuple[T, Any]:
                """锁定配置行，把关联写入与最终配置值放入同一事务。"""
                conf = db.execute(
                    select(SystemConfig)
                    .where(SystemConfig.key == key)
                    .with_for_update()
                ).scalar_one_or_none()
                current = copy.deepcopy(conf.value if conf else None)
                result, value = mutation(db, current)
                committed_value = copy.deepcopy(value)
                if conf:
                    conf.value = committed_value
                else:
                    db.add(SystemConfig(key=key, value=committed_value))
                return result, committed_value

            result, committed_value = self._execute_sync_write(write)
            self._publish_value(key, committed_value)
            return result

    def get(self, key: Optional[Union[str, SystemConfigKey]] = None) -> Any:
        """
        获取系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        if not key:
            return self.all()
        with self._snapshot_lock:
            self._require_loaded()
            # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
            return copy.deepcopy(self.__SYSTEMCONF.get(key))

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """
        原子递增整数系统设置

        :param key: 配置键
        :param step: 递增步长
        :return: 递增后的整数值
        """
        self._require_loaded()
        with self._write_lock:
            value = int(self.get(key) or 0) + step
            self.set(key, value)
            return value

    def all(self) -> dict[str, Any]:
        """
        获取所有系统设置
        """
        with self._snapshot_lock:
            self._require_loaded()
            # 避免将__SYSTEMCONF内的值引用出去，会导致set时误判没有变动
            return copy.deepcopy(self.__SYSTEMCONF)

    def delete(self, key: Union[str, SystemConfigKey]) -> bool:
        """
        删除系统设置
        """
        if isinstance(key, SystemConfigKey):
            key = key.value
        self._require_loaded()
        with self._write_lock:

            def delete(db):
                """在当前事务中删除配置记录。"""
                conf = SystemConfig.get_by_key(db, key)
                if conf:
                    db.delete(conf)

            self._execute_sync_write(delete)
            self._publish_delete(key)
            return True
