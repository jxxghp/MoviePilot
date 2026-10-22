"""
验证同步和异步卸载在真实SQLite清单上的原子成员更新。
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.api.endpoints import plugin as endpoint
from app.application import scheduling
from app.application.configuration import SystemConfigService
from app.application.plugin import folders, management, routes
from app.db.models.systemconfig import SystemConfig
from app.schemas.types import SystemConfigKey
from tests.test_plugin_install_transaction import _AtomicSystemConfig


class _MembershipConfig(_AtomicSystemConfig):
    """
    在真实SQLite中提供清单读写，旧分离读写在屏障处稳定产生丢更新。

    :ivar readers: 将旧两个清单快照固定在同一竞争窗口的屏障
    """

    def __init__(self, factory):
        """
        保存独立会话工厂和竞争窗口。
        """
        super().__init__(factory)
        self.readers = threading.Barrier(2)

    def get(self, key):
        """
        读取已提交清单，在旧实现的首次快照之间同步。
        """
        with self._factory() as session:
            result = deepcopy(session.execute(sa.select(SystemConfig.value).where(
                SystemConfig.key == key.value,
            )).scalar_one())
        self.readers.wait(5)
        return result

    def set(self, key, value):
        """
        兼容旧接口的独立全量覆盖写入。
        """
        with self._factory.begin() as session:
            session.execute(sa.update(SystemConfig).where(SystemConfig.key == key.value).values(value=value))
        return True

    def update_atomically(self, key, mutation):
        """
        与安装事务使用相同配置写锁和短事务更新成员。
        """
        return super().update_atomically(key.value, mutation)


class _ThreadExecutor:
    """
    提供与生产异步数据库执行器相同的独立线程调用边界。
    """

    async def run(self, operation):
        """
        等待单个短事务结束。
        """
        return await asyncio.to_thread(operation)


@pytest.mark.parametrize("paths", [("sync", "sync"), ("sync", "async"), ("async", "async")])
def test_concurrent_uninstalls_preserve_other_members_and_install_update(paths, tmp_path, monkeypatch):
    """
    两条卸载入口删除不同插件时均保留另一请求及后续原子安装的变更。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'membership.db'}", connect_args={"timeout": 5})
    SystemConfig.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(SystemConfig(key=SystemConfigKey.UserInstalledPlugins.value,
                                 value=["A", "B", "Unrelated"]))
    store = _MembershipConfig(factory)
    config = SystemConfigService(reader=store, writer=store, async_executor=_ThreadExecutor())
    manager = Mock()
    manager.plugins = {}
    manager.mutation.side_effect = lambda _: nullcontext()
    manager.get_plugin_instance.return_value = None
    manager.get_plugin_source_instances.return_value = []
    manager.stop.return_value = None
    monkeypatch.setattr(endpoint, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(management, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(endpoint, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(management, "get_configured_system_config", lambda: config)
    for module, names in [(endpoint, ["remove_plugin_api", "remove_plugin_job", "remove_plugin_from_folders"]),
                          (routes, ["remove_plugin_api"]), (scheduling, ["remove_plugin_job"]),
                          (folders, ["remove_plugin_from_folders"])]:
        for name in names:
            monkeypatch.setattr(module, name, lambda _: None)

    def uninstall(arguments):
        """
        每个请求持有自己的调用线程及异步事件循环。
        """
        path, plugin_id = arguments
        if path == "sync":
            assert endpoint.uninstall_plugin(plugin_id, None).success
        else:
            asyncio.run(management.uninstall_plugin_runtime(plugin_id))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(uninstall, zip(paths, ["A", "B"])))
        with factory() as session:
            assert session.execute(sa.select(SystemConfig.value)).scalar_one() == ["Unrelated"]
        config.update_atomically(SystemConfigKey.UserInstalledPlugins,
                                 lambda current: (None, [*current, "InstalledLater"]))
        with factory() as session:
            assert session.execute(sa.select(SystemConfig.value)).scalar_one() == ["Unrelated", "InstalledLater"]
    finally:
        engine.dispose()
