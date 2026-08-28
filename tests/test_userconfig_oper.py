"""用户配置快照与异步写入合同测试。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.application.security.userconfig import UserConfigurationService
from app.db.adapters.configuration import TransactionalUserConfigurationRepository
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.oper.userconfig import UserConfigOper
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyUnitOfWork
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
    with SessionFactory() as session:
        oper.load_snapshot(session)
    return oper


def _fresh_repository() -> tuple[
    TransactionalUserConfigurationRepository,
    UserConfigOper,
]:
    """构造使用新快照单例的用户配置短事务仓储。"""
    oper = _fresh_oper()
    return TransactionalUserConfigurationRepository(SessionFactory, oper), oper


def _add_users(db, *usernames: str) -> None:
    """为配置用例创建受外键保护的真实用户主体。"""
    db.watermark(User)
    db.add(*[User(name=username, is_active=True, is_superuser=False) for username in usernames])


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
async def test_async_write_uses_same_repository_rule(db) -> None:
    """异步入口提交后同步读取立即看到相同结果。"""
    _add_users(db, "async-user")
    repository, _oper = _fresh_repository()
    service = UserConfigurationService(
        repository,
        async_executor=_ThreadDatabaseExecutor(),
    )

    await service.async_set("async-user", "theme", "dark")

    assert service.get("async-user", "theme") == "dark"
    assert (
        UserConfig.get_by_key(
            db.session,
            username="async-user",
            key="theme",
        ).value
        == "dark"
    )


def test_existing_falsey_value_removes_database_and_snapshot_entry(db) -> None:
    """已有用户配置写入假值时，数据库记录与快照同步移除。"""
    db.watermark(UserConfig)
    _add_users(db, "falsey-user")
    oper = _fresh_oper()

    oper.set("falsey-user", "enabled", True)
    oper.set("falsey-user", "enabled", False)

    assert (
        UserConfig.get_by_key(
            db.session,
            username="falsey-user",
            key="enabled",
        )
        is None
    )
    assert oper.get("falsey-user", "enabled") is None


def test_falsey_value_without_existing_row_is_persisted(db) -> None:
    """不存在的用户配置写入假值时保留记录，兼容历史写入规则。"""
    db.watermark(UserConfig)
    _add_users(db, "new-falsey-user")
    oper = _fresh_oper()

    oper.set("new-falsey-user", "enabled", False)

    persisted = UserConfig.get_by_key(
        db.session,
        username="new-falsey-user",
        key="enabled",
    )
    assert persisted is not None
    assert persisted.value is False
    assert oper.get("new-falsey-user", "enabled") is False


def test_repository_deeply_isolates_inputs_and_outputs(db) -> None:
    """嵌套可变配置在写入和读取两端都不得泄漏快照内部引用。"""
    db.watermark(UserConfig)
    _add_users(db, "isolated-user")
    repository, _oper = _fresh_repository()
    payload = {"nested": {"items": ["original"]}}

    repository.set("isolated-user", "layout", payload)
    payload["nested"]["items"].append("input-mutated")
    first = repository.get("isolated-user", "layout")
    assert first == {"nested": {"items": ["original"]}}

    first["nested"]["items"].append("output-mutated")
    assert repository.get("isolated-user", "layout") == {"nested": {"items": ["original"]}}
    db.session.expire_all()
    assert UserConfig.get_by_key(
        db.session,
        username="isolated-user",
        key="layout",
    ).value == {"nested": {"items": ["original"]}}


def test_commit_failure_never_publishes_snapshot(db, monkeypatch) -> None:
    """数据库提交失败必须回滚暂存值，并保持既有快照不变。"""
    db.watermark(UserConfig)
    _add_users(db, "commit-user")
    repository, oper = _fresh_repository()
    repository.set("commit-user", "theme", "old")
    published = threading.Event()
    original_publish = oper.publish

    def track_publish(*args, **kwargs) -> None:
        """记录任何不应发生的提交后发布。"""
        published.set()
        original_publish(*args, **kwargs)

    def fail_commit(_unit_of_work) -> None:
        """模拟数据库在提交边界失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(oper, "publish", track_publish)
    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        repository.set("commit-user", "theme", "new")

    assert published.is_set() is False
    assert repository.get("commit-user", "theme") == "old"
    db.session.expire_all()
    assert (
        UserConfig.get_by_key(
            db.session,
            username="commit-user",
            key="theme",
        ).value
        == "old"
    )


def test_publish_failure_reloads_committed_database_value(db, monkeypatch) -> None:
    """提交成功但增量发布失败时，从数据库重载快照后再传播异常。"""
    db.watermark(UserConfig)
    _add_users(db, "reload-user")
    repository, oper = _fresh_repository()
    repository.set("reload-user", "theme", "old")

    def fail_publish(*_args, **_kwargs) -> None:
        """模拟提交后的内存快照发布失败。"""
        raise RuntimeError("publish failed")

    monkeypatch.setattr(oper, "publish", fail_publish)

    with pytest.raises(RuntimeError, match="publish failed"):
        repository.set("reload-user", "theme", "committed")

    assert repository.get("reload-user", "theme") == "committed"
    db.session.expire_all()
    assert (
        UserConfig.get_by_key(
            db.session,
            username="reload-user",
            key="theme",
        ).value
        == "committed"
    )


def test_rename_publish_failure_reloads_committed_database_state(
    db,
    monkeypatch,
) -> None:
    """改名已提交但快照迁移失败时，重载后只能看到新用户名配置。"""
    db.watermark(UserConfig)
    _add_users(db, "old-name")
    repository, oper = _fresh_repository()
    repository.set("old-name", "theme", "dark")
    user = User.get_by_name(db.session, "old-name")
    user.name = "new-name"
    db.session.commit()

    def fail_publish(*_args, **_kwargs) -> None:
        """模拟改名提交后的增量快照发布失败。"""
        raise RuntimeError("rename publish failed")

    monkeypatch.setattr(oper, "publish_rename", fail_publish)

    with pytest.raises(RuntimeError, match="rename publish failed"):
        repository.publish_rename("old-name", "new-name")

    assert repository.get("old-name", "theme") is None
    assert repository.get("new-name", "theme") == "dark"


def test_delete_publish_failure_reloads_committed_database_state(
    db,
    monkeypatch,
) -> None:
    """用户删除已提交但快照删除失败时，重载后不得残留旧用户名配置。"""
    db.watermark(UserConfig)
    _add_users(db, "deleted-user")
    repository, oper = _fresh_repository()
    repository.set("deleted-user", "theme", "dark")
    db.session.delete(User.get_by_name(db.session, "deleted-user"))
    db.session.commit()

    def fail_publish(*_args, **_kwargs) -> None:
        """模拟删除提交后的增量快照发布失败。"""
        raise RuntimeError("delete publish failed")

    monkeypatch.setattr(oper, "publish_delete", fail_publish)

    with pytest.raises(RuntimeError, match="delete publish failed"):
        repository.publish_delete("deleted-user")

    assert repository.get("deleted-user", "theme") is None


def test_concurrent_writes_keep_database_and_snapshot_consistent(db) -> None:
    """同一配置的并发短事务按提交顺序发布，最终快照与数据库一致。"""
    db.watermark(UserConfig)
    _add_users(db, "concurrent-user")
    repository, _oper = _fresh_repository()
    values = [{"revision": revision} for revision in range(12)]

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(
            executor.map(
                lambda value: repository.set("concurrent-user", "layout", value),
                values,
            )
        )

    db.session.expire_all()
    persisted = UserConfig.get_by_key(
        db.session,
        username="concurrent-user",
        key="layout",
    )
    assert persisted is not None
    assert repository.get("concurrent-user", "layout") == persisted.value
