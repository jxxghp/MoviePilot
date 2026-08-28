import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.schemas.types import SystemConfigKey
from app.foundation.singleton import Singleton


def _unique_key() -> str:
    """生成独立测试键，避免用例间相互污染。"""
    return f"__test__{uuid.uuid4().hex}"


def _fresh_oper() -> SystemConfigOper:
    """重置单例并显式加载数据库配置快照。"""
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    oper = SystemConfigOper()
    oper.load_snapshot()
    return oper


def _stored_config(key: str) -> SystemConfig | None:
    """使用显式短会话回读系统配置持久化结果。"""
    with SessionFactory() as session:
        return SystemConfig.get_by_key(session, key)


def test_constructor_does_not_query_database(monkeypatch):
    """构造配置对象时不打开数据库会话。"""
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    monkeypatch.setattr(
        SystemConfig,
        "list",
        lambda _db: pytest.fail("构造阶段不应查询数据库"),
    )

    oper = SystemConfigOper()

    with pytest.raises(RuntimeError, match="快照尚未加载"):
        oper.get("key")


def test_load_snapshot_publishes_complete_dictionary(monkeypatch):
    """重新加载期间读取方只会看到完整旧快照或完整新快照。"""
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    oper = SystemConfigOper()
    values = [SimpleNamespace(key="key", value="old")]
    entered = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(SystemConfig, "list", lambda _db: values)
    oper.load_snapshot()

    def load_new_snapshot(_db):
        entered.set()
        release.wait(1)
        return [SimpleNamespace(key="key", value="new")]

    monkeypatch.setattr(SystemConfig, "list", load_new_snapshot)
    thread = threading.Thread(target=oper.load_snapshot)
    thread.start()
    assert entered.wait(1)

    assert oper.get("key") == "old"

    release.set()
    thread.join(1)
    assert thread.is_alive() is False
    assert oper.get("key") == "new"


def test_read_does_not_wait_for_slow_write_transaction(monkeypatch):
    """数据库写入期间，内存读取仍返回最近一次已提交值。"""
    oper = _fresh_oper()
    key = _unique_key()
    oper.set(key, "old")
    entered = threading.Event()
    release = threading.Event()

    def slow_write(_operation):
        entered.set()
        release.wait(1)
        return True

    monkeypatch.setattr(oper, "_execute_sync_write", slow_write)
    thread = threading.Thread(target=lambda: oper.set(key, "new"))
    thread.start()
    assert entered.wait(1)

    assert oper.get(key) == "old"

    release.set()
    thread.join(1)
    assert thread.is_alive() is False
    assert oper.get(key) == "new"


def test_failed_write_keeps_committed_snapshot(monkeypatch):
    """事务失败时内存快照保持最近一次已提交值。"""
    oper = _fresh_oper()
    key = _unique_key()
    oper.set(key, "old")

    def fail_write(_operation):
        raise RuntimeError("write failed")

    monkeypatch.setattr(oper, "_execute_sync_write", fail_write)

    with pytest.raises(RuntimeError, match="write failed"):
        oper.set(key, "new")
    assert oper.get(key) == "old"


def test_update_atomically_commits_related_records_and_snapshot() -> None:
    """关联记录与最终配置值必须在同一事务成功后一起可见。"""
    key = _unique_key()
    related_key = _unique_key()
    oper = _fresh_oper()
    oper.set(key, ["ExistingPlugin"])

    def mutation(session, current):
        session.add(SystemConfig(key=related_key, value={"phase": "committed"}))
        return "done", [*current, "DemoPlugin"]

    assert oper.update_atomically(key, mutation) == "done"
    assert oper.get(key) == ["ExistingPlugin", "DemoPlugin"]
    assert _stored_config(key).value == ["ExistingPlugin", "DemoPlugin"]
    assert _stored_config(related_key).value == {"phase": "committed"}


def test_update_atomically_keeps_snapshot_when_related_write_fails() -> None:
    """关联写失败时配置数据库值和内存快照都保持最近提交状态。"""
    key = _unique_key()
    related_key = _unique_key()
    oper = _fresh_oper()
    oper.set(key, ["ExistingPlugin"])

    def mutation(session, _current):
        session.add(SystemConfig(key=related_key, value=True))
        raise RuntimeError("related write failed")

    with pytest.raises(RuntimeError, match="related write failed"):
        oper.update_atomically(key, mutation)

    assert oper.get(key) == ["ExistingPlugin"]
    assert _stored_config(key).value == ["ExistingPlugin"]
    assert _stored_config(related_key) is None


def test_increment_serializes_concurrent_counter_updates(monkeypatch):
    """并发递增系统计数时不应丢失更新。"""
    oper = object.__new__(SystemConfigOper)
    oper._snapshot_lock = threading.RLock()
    oper._write_lock = threading.RLock()
    oper._loaded = True
    stored_value = {"value": 0}

    monkeypatch.setattr(oper, "get", lambda _key: stored_value["value"])
    monkeypatch.setattr(
        oper,
        "set",
        lambda _key, value: stored_value.update(value=value),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: oper.increment(
                    SystemConfigKey.MediaRecognizeShareCount
                ),
                range(100),
            )
        )

    assert sorted(results) == list(range(1, 101))
    assert stored_value["value"] == 100


def test_increment_supports_custom_step(monkeypatch):
    """整数系统计数应支持指定递增步长。"""
    oper = object.__new__(SystemConfigOper)
    oper._snapshot_lock = threading.RLock()
    oper._write_lock = threading.RLock()
    oper._loaded = True
    stored_value = {"value": 4}

    monkeypatch.setattr(oper, "get", lambda _key: stored_value["value"])
    monkeypatch.setattr(
        oper,
        "set",
        lambda _key, value: stored_value.update(value=value),
    )

    result = oper.increment(SystemConfigKey.MediaRecognizeShareCount, step=3)

    assert result == 7
    assert stored_value["value"] == 7


def test_set_creates_record_for_falsy_value():
    """无记录时写入假值应创建记录，而不是丢弃配置。"""
    key = _unique_key()
    oper = _fresh_oper()

    assert oper.set(key, False) is True
    assert oper.get(key) is False
    assert _stored_config(key) is not None


def test_set_persists_falsy_value_on_existing_record():
    """已存在记录时写入假值应更新记录，而不是删除记录回落默认值。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, True)
    assert oper.set(key, False) is True
    assert oper.get(key) is False
    assert _stored_config(key).value is False


@pytest.mark.asyncio
async def test_async_set_persists_falsy_value_on_existing_record():
    """异步写入假值同样应更新记录而不是删除记录。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, True)
    from app.application.configuration import SystemConfigService

    class _InlineDatabaseExecutor:
        async def run(self, operation):
            return operation()

    service = SystemConfigService(
        repository=oper,
        async_executor=_InlineDatabaseExecutor(),
    )
    assert await service.async_set(key, False) is True
    assert oper.get(key) is False
    assert _stored_config(key).value is False


@pytest.mark.asyncio
async def test_async_set_creates_record_for_falsy_value():
    """异步写入假值且无记录时应创建记录。"""
    key = _unique_key()
    oper = _fresh_oper()

    from app.application.configuration import SystemConfigService

    class _InlineDatabaseExecutor:
        async def run(self, operation):
            return operation()

    service = SystemConfigService(
        repository=oper,
        async_executor=_InlineDatabaseExecutor(),
    )
    assert await service.async_set(key, 0) is True
    assert oper.get(key) == 0
    assert _stored_config(key).value == 0


def test_delete_removes_record_explicitly():
    """显式 delete 仍是删除配置的唯一途径。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, False)
    assert oper.delete(key) is True
    assert oper.get(key) is None
    assert _stored_config(key) is None


def test_mounted_local_disk_delete_empty_dirs_off_is_persisted():
    """回归：挂载盘删除空目录开关关闭后，读取端不应回落默认开启（issue #6309）。"""
    key = SystemConfigKey.MountedLocalDiskDeleteEmptyDirs.value
    oper = _fresh_oper()

    # 模拟前端保存关闭：已有记录（开启）→ 关闭
    oper.set(key, True)
    oper.set(key, False)
    # 复刻 app/chain/transfer/ 的读取语义：无记录视为默认开启
    assert (oper.get(key) is not False) is False

    # 模拟全新安装首次保存关闭：无记录 → 创建关闭记录
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    oper.delete(key)
    _fresh_oper().set(key, False)
    assert _fresh_oper().get(key) is False
