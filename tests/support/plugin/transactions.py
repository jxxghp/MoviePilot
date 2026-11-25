"""
插件安装记录与原子配置替身；不注册测试用例。
"""

import copy
import threading
from datetime import datetime, timezone
import sqlalchemy as sa
from app.application.plugin.transaction import PluginInstallationPhase, PluginInstallationRecord
from app.db.models.systemconfig import SystemConfig


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _record(**overrides) -> PluginInstallationRecord:
    """
    构造可跨进程恢复的安装事务记录。
    """
    values = {
        "transaction_id": "txn-demo-1",
        "plugin_id": "DemoPlugin",
        "phase": PluginInstallationPhase.PREPARED,
        "membership_before": True,
        "membership_target": None,
        "identity_before_revision": 1,
        "identity_target_revision": None,
        "package_existed": True,
        "persistent_backup_existed": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return PluginInstallationRecord(**values)


class _AtomicSystemConfig:
    """
    用测试 Session 模拟 SystemConfigOper 的配置锁和原子提交。
    """

    def __init__(self, factory) -> None:
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param factory: 原子配置读写工厂
        """
        self._factory = factory
        self._lock = threading.RLock()

    def update_atomically(self, key, mutation):
        """
        在测试数据库事务中锁定配置并执行关联写入。
        """
        with self._lock:
            session = self._factory()
            try:
                with session.begin():
                    config = session.execute(
                        sa.select(SystemConfig)
                        .where(SystemConfig.key == key)
                        .with_for_update()
                    ).scalar_one_or_none()
                    current = copy.deepcopy(config.value if config else None)
                    result, value = mutation(session, current)
                    if config is None:
                        session.add(SystemConfig(key=key, value=copy.deepcopy(value)))
                    else:
                        config.value = copy.deepcopy(value)
                    session.flush()
                    return result
            finally:
                session.close()
