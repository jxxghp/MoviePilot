"""
验证插件同键持久化、源码资源热更新与公开文件缓存兼容回归。
"""

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.models.plugindata import PluginData
from app.db.oper.plugindata import PluginDataOper
from app.runtime.compat.resources import scan_plugin_resource_imports
from app.sdk.cache import AsyncFileBackend, FileBackend


def test_concurrent_plugin_key_save_has_one_sync_and_async_value(tmp_path):
    """
    同步首次保存同键的竞争不能产生重复行，异步读取必须返回同一有效值。
    """
    database = tmp_path / "plugin-data.db"
    engine = sa.create_engine(f"sqlite:///{database}", connect_args={"timeout": 10})
    PluginData.__table__.create(engine)
    first_reads = Barrier(2)
    writers = Barrier(2)

    def synchronize_legacy_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
        """
        将旧版读后插入的两个首次查询固定在同一竞争窗口。
        """
        if statement.lstrip().upper().startswith("SELECT") and "plugindata" in statement:
            first_reads.wait(5)

    sa.event.listen(engine, "after_cursor_execute", synchronize_legacy_reads)

    def save(value):
        """
        每个写者持有独立事务，提交由调用方负责。
        """
        writers.wait(5)
        with Session(engine) as session, session.begin():
            PluginDataOper(session).save("Demo", "key", value)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(save, ["A", "B"]))
    finally:
        sa.event.remove(engine, "after_cursor_execute", synchronize_legacy_reads)
    with Session(engine) as session:
        rows = session.execute(sa.select(PluginData)).scalars().all()
        assert len(rows) == 1
        expected = PluginDataOper(session).get_data("Demo", "key")
        assert expected in {"A", "B"}

    async def read_async():
        """
        用独立异步会话核对同一物理数据文件。
        """
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(async_engine)() as session:
                return await PluginDataOper(session).async_get_data("Demo", "key")
        finally:
            await async_engine.dispose()

    assert asyncio.run(read_async()) == expected
    engine.dispose()


def test_plugin_key_unique_migration_preserves_previously_visible_value(monkeypatch):
    """
    升级去除旧重复行并保留同步读取原先可见的最低ID值，降级只移除唯一约束。
    """
    migration = importlib.import_module("database.versions.b8d2f5a01962_plugin_data_identity")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("plugindata", metadata,
                     sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("plugin_id", sa.String, nullable=False),
                     sa.Column("key", sa.String, nullable=False), sa.Column("value", sa.JSON))
    sa.Index("ix_plugindata_plugin_id_key", table.c.plugin_id, table.c.key)
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(table.insert(), [
            {"id": 1, "plugin_id": "Demo", "key": "key", "value": "visible"},
            {"id": 2, "plugin_id": "Demo", "key": "key", "value": "duplicate"},
            {"id": 3, "plugin_id": "Other", "key": "key", "value": "other"},
        ])
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(sa.select(table).order_by(table.c.id)).mappings().all()
        assert [(row["plugin_id"], row["value"]) for row in rows] == [("Demo", "visible"), ("Other", "other")]
        index = next(index for index in sa.inspect(connection).get_indexes("plugindata")
                     if index["name"] == "ix_plugindata_plugin_id_key")
        assert index["unique"]
        migration.downgrade()
        index = next(index for index in sa.inspect(connection).get_indexes("plugindata")
                     if index["name"] == "ix_plugindata_plugin_id_key")
        assert not index["unique"]
    engine.dispose()


@pytest.mark.asyncio
async def test_async_concurrent_upsert_and_caller_rollback_are_visible_consistently(tmp_path):
    """
    异步首次写入也不能重复，已加载模型刷新但调用方回滚仍恢复原值。
    """
    database = tmp_path / "async-plugin-data.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", connect_args={"timeout": 10})
    async with engine.begin() as connection:
        await connection.run_sync(PluginData.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ready = asyncio.Event()

    async def save(value):
        """
        每个异步写者使用独立事务，并在共同事件后开始。
        """
        async with sessions() as session, session.begin():
            await ready.wait()
            await PluginDataOper(session).async_save("Demo", "key", value)

    try:
        writers = [asyncio.create_task(save(value)) for value in ("first", "second")]
        ready.set()
        await asyncio.gather(*writers)
        async with sessions() as session:
            rows = (await session.execute(sa.select(PluginData))).scalars().all()
            assert len(rows) == 1
            original = rows[0].value
            await PluginDataOper(session).async_save("Demo", "key", "temporary")
            assert rows[0].value == "temporary"
            await session.rollback()
        async with sessions() as session:
            assert await PluginDataOper(session).async_get_data("Demo", "key") == original
    finally:
        await engine.dispose()


def test_sync_upsert_refreshes_loaded_model_without_committing(tmp_path):
    """
    同步upsert更新调用方已加载对象，同时保留调用方对提交和回滚的完整控制。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    PluginData.__table__.create(engine)
    try:
        with Session(engine) as session, session.begin():
            PluginDataOper(session).save("Demo", "key", "original")
        with Session(engine) as session:
            loaded = session.execute(sa.select(PluginData)).scalar_one()
            PluginDataOper(session).save("Demo", "key", "temporary")
            assert loaded.value == "temporary"
            session.rollback()
        with Session(engine) as session:
            assert PluginDataOper(session).get_data("Demo", "key") == "original"
    finally:
        engine.dispose()


def test_resource_scan_observes_equal_size_change_with_identical_stat(tmp_path, monkeypatch):
    """
    热更新在相同时间粒度内等长改写时，缓存必须按实际内容失效，无需sleep。
    """
    plugin = tmp_path / "Demo"
    plugin.mkdir()
    source = plugin / "__init__.py"
    source.write_text("import cloakbrowsex\n", encoding="utf-8")
    initial = source.stat()
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs: initial if path == source else real_stat(path, *args, **kwargs))
    assert scan_plugin_resource_imports("Demo", plugin) == ()
    source.write_text("import cloakbrowser\n", encoding="utf-8")
    assert scan_plugin_resource_imports("Demo", plugin) == ("host.display",)
    source.write_text("import cloakbrowsex\n", encoding="utf-8")
    assert scan_plugin_resource_imports("Demo", plugin) == ()


def test_sdk_file_cache_preserves_third_positional_region(tmp_path):
    """
    公开同步文件后端沿用旧第三位置region，同时接纳新ttl关键字。
    """
    cache = FileBackend(tmp_path)
    cache.set("same", b"images", "images")
    cache.set("same", b"lyrics", "lyrics")
    cache.set("new", b"ttl", region="images", ttl=30)
    assert cache.get("same", "images") == b"images"
    assert cache.get("same", "lyrics") == b"lyrics"
    assert cache.get("new", "images") == b"ttl"


@pytest.mark.asyncio
async def test_sdk_async_file_cache_preserves_third_positional_region(tmp_path):
    """
    公开异步文件后端的旧第三位置region不再混写默认区域。
    """
    cache = AsyncFileBackend(tmp_path)
    await cache.set("same", b"images", "images")
    await cache.set("same", b"lyrics", "lyrics")
    await cache.set("new", b"ttl", region="images", ttl=30)
    assert await cache.get("same", "images") == b"images"
    assert await cache.get("same", "lyrics") == b"lyrics"
    assert await cache.get("new", "images") == b"ttl"
