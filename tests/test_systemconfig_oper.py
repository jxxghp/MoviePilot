import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db.models.systemconfig import SystemConfig
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
from app.foundation.singleton import Singleton


def _unique_key() -> str:
    """生成独立测试键，避免用例间相互污染。"""
    return f"__test__{uuid.uuid4().hex}"


def _fresh_oper() -> SystemConfigOper:
    """重置单例并从数据库重新加载配置缓存。"""
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    return SystemConfigOper()


def test_increment_serializes_concurrent_counter_updates(monkeypatch):
    """并发递增系统计数时不应丢失更新。"""
    oper = object.__new__(SystemConfigOper)
    oper._rlock = threading.RLock()
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
    oper._rlock = threading.RLock()
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


def test_get_many_reads_one_locked_cache_snapshot():
    """并发更新多个键时，批量读取不得返回新旧值混合的快照。"""
    first_key = _unique_key()
    second_key = _unique_key()
    first_read = threading.Event()
    writer_attempted = threading.Event()

    class CoordinatedCache(dict):
        """在首键读取后让写线程尝试获得同一把缓存锁。"""

        def get(self, key, default=None):
            value = super().get(key, default)
            if key == first_key:
                first_read.set()
                assert writer_attempted.wait(timeout=1)
            return value

    oper = object.__new__(SystemConfigOper)
    oper._rlock = threading.RLock()
    oper._SystemConfigOper__SYSTEMCONF = CoordinatedCache({
        first_key: "old",
        second_key: "old",
    })

    def update_snapshot() -> None:
        assert first_read.wait(timeout=1)
        writer_attempted.set()
        with oper._rlock:
            oper._SystemConfigOper__SYSTEMCONF[first_key] = "new"
            oper._SystemConfigOper__SYSTEMCONF[second_key] = "new"

    writer = threading.Thread(target=update_snapshot)
    writer.start()
    snapshot = oper.get_many((first_key, second_key))
    writer.join(timeout=1)

    assert not writer.is_alive()
    assert snapshot == {first_key: "old", second_key: "old"}
    assert oper.get_many((first_key, second_key)) == {
        first_key: "new",
        second_key: "new",
    }


def test_set_creates_record_for_falsy_value():
    """无记录时写入假值应创建记录，而不是丢弃配置。"""
    key = _unique_key()
    oper = _fresh_oper()

    assert oper.set(key, False) is True
    assert oper.get(key) is False
    assert SystemConfig.get_by_key(oper._db, key) is not None


def test_set_persists_falsy_value_on_existing_record():
    """已存在记录时写入假值应更新记录，而不是删除记录回落默认值。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, True)
    assert oper.set(key, False) is True
    assert oper.get(key) is False
    assert SystemConfig.get_by_key(oper._db, key).value is False


@pytest.mark.asyncio
async def test_async_set_persists_falsy_value_on_existing_record():
    """异步写入假值同样应更新记录而不是删除记录。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, True)
    assert await oper.async_set(key, False) is True
    assert oper.get(key) is False
    assert SystemConfig.get_by_key(oper._db, key).value is False


@pytest.mark.asyncio
async def test_async_set_creates_record_for_falsy_value():
    """异步写入假值且无记录时应创建记录。"""
    key = _unique_key()
    oper = _fresh_oper()

    assert await oper.async_set(key, 0) is True
    assert oper.get(key) == 0
    assert SystemConfig.get_by_key(oper._db, key).value == 0


@pytest.mark.asyncio
async def test_async_set_many_commits_values_together():
    """批量设置应在一次成功事务后同时更新数据库和缓存。"""
    first_key = _unique_key()
    second_key = _unique_key()
    oper = _fresh_oper()

    changed = await oper.async_set_many({first_key: [1], second_key: "specificity"})

    assert changed == {first_key, second_key}
    assert oper.get(first_key) == [1]
    assert oper.get(second_key) == "specificity"
    assert SystemConfig.get_by_key(oper._db, first_key).value == [1]
    assert SystemConfig.get_by_key(oper._db, second_key).value == "specificity"


@pytest.mark.asyncio
async def test_async_set_many_rolls_back_every_value_on_failure():
    """任一配置无法持久化时不得留下部分数据库或缓存更新。"""
    first_key = _unique_key()
    second_key = _unique_key()
    oper = _fresh_oper()
    oper.set(first_key, "before-first")
    oper.set(second_key, "before-second")

    with pytest.raises(Exception):
        await oper.async_set_many({first_key: "after", second_key: object()})

    assert oper.get(first_key) == "before-first"
    assert oper.get(second_key) == "before-second"
    assert SystemConfig.get_by_key(oper._db, first_key).value == "before-first"
    assert SystemConfig.get_by_key(oper._db, second_key).value == "before-second"


def test_delete_removes_record_explicitly():
    """显式 delete 仍是删除配置的唯一途径。"""
    key = _unique_key()
    oper = _fresh_oper()

    oper.set(key, False)
    assert oper.delete(key) is True
    assert oper.get(key) is None
    assert SystemConfig.get_by_key(oper._db, key) is None


def test_mounted_local_disk_delete_empty_dirs_off_is_persisted():
    """回归：挂载盘删除空目录开关关闭后，读取端不应回落默认开启（issue #6309）。"""
    key = SystemConfigKey.MountedLocalDiskDeleteEmptyDirs.value
    oper = _fresh_oper()

    # 模拟前端保存关闭：已有记录（开启）→ 关闭
    oper.set(key, True)
    oper.set(key, False)
    # 复刻 app/chain/transfer.py 的读取语义：无记录视为默认开启
    assert (oper.get(key) is not False) is False

    # 模拟全新安装首次保存关闭：无记录 → 创建关闭记录
    Singleton._instances.pop((SystemConfigOper, (), frozenset()), None)
    oper.delete(key)
    _fresh_oper().set(key, False)
    assert _fresh_oper().get(key) is False
