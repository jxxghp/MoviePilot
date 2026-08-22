"""用户配置快照与异步写入合同测试。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.application.security.userconfig import UserConfigurationService
from app.db.models.userconfig import UserConfig
from app.db.oper.userconfig import UserConfigOper
from app.foundation.singleton import Singleton


class _ThreadDatabaseExecutor:
    """在线程中执行测试事务。"""

    async def run(self, operation):
        """执行并返回操作结果。"""
        return await asyncio.to_thread(operation)


def _fresh_oper() -> UserConfigOper:
    """重置单例并显式加载用户配置快照。"""
    Singleton._instances.pop((UserConfigOper, (), frozenset()), None)
    oper = UserConfigOper()
    oper.load_snapshot()
    return oper


def test_constructor_does_not_query_database(monkeypatch):
    """构造用户配置对象时不打开数据库会话。"""
    Singleton._instances.pop((UserConfigOper, (), frozenset()), None)
    monkeypatch.setattr(
        UserConfig,
        "list",
        lambda _db: pytest.fail("构造阶段不应查询数据库"),
    )

    oper = UserConfigOper()

    with pytest.raises(RuntimeError, match="快照尚未加载"):
        oper.get("alice", "theme")


def test_load_snapshot_publishes_complete_dictionary(monkeypatch):
    """重新加载期间读取方不会看到逐项构造的用户配置。"""
    Singleton._instances.pop((UserConfigOper, (), frozenset()), None)
    oper = UserConfigOper()
    monkeypatch.setattr(
        UserConfig,
        "list",
        lambda _db: [SimpleNamespace(username="alice", key="theme", value="old")],
    )
    oper.load_snapshot()
    entered = threading.Event()
    release = threading.Event()

    def load_new_snapshot(_db):
        entered.set()
        release.wait(1)
        return [SimpleNamespace(username="alice", key="theme", value="new")]

    monkeypatch.setattr(UserConfig, "list", load_new_snapshot)
    thread = threading.Thread(target=oper.load_snapshot)
    thread.start()
    assert entered.wait(1)

    assert oper.get("alice", "theme") == "old"

    release.set()
    thread.join(1)
    assert thread.is_alive() is False
    assert oper.get("alice", "theme") == "new"


@pytest.mark.asyncio
async def test_async_write_uses_same_repository_rule() -> None:
    """异步入口提交后同步读取立即看到相同结果。"""
    oper = _fresh_oper()
    service = UserConfigurationService(
        oper,
        async_executor=_ThreadDatabaseExecutor(),
    )

    await service.async_set("async-user", "theme", "dark")

    assert service.get("async-user", "theme") == "dark"
    assert UserConfig.get_by_key(
        oper._db,
        username="async-user",
        key="theme",
    ).value == "dark"


def test_existing_falsey_value_is_removed_from_db_but_kept_until_reload(db) -> None:
    """已有用户配置写入假值时删除记录，当前快照仍保留该假值直到重载。"""
    db.watermark(UserConfig)
    oper = _fresh_oper()

    oper.set("falsey-user", "enabled", True)
    oper.set("falsey-user", "enabled", False)

    assert UserConfig.get_by_key(
        oper._db,
        username="falsey-user",
        key="enabled",
    ) is None
    assert oper.get("falsey-user", "enabled") is False

    oper.load_snapshot()
    assert oper.get("falsey-user", "enabled") is None


def test_falsey_value_without_existing_row_is_persisted(db) -> None:
    """不存在的用户配置写入假值时保留记录，兼容历史写入规则。"""
    db.watermark(UserConfig)
    oper = _fresh_oper()

    oper.set("new-falsey-user", "enabled", False)

    persisted = UserConfig.get_by_key(
        oper._db,
        username="new-falsey-user",
        key="enabled",
    )
    assert persisted is not None
    assert persisted.value is False
    assert oper.get("new-falsey-user", "enabled") is False
