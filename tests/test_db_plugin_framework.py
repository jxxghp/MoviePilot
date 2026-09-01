"""插件自管理数据库框架：建表隔离、SQLite 生命周期与 PostgreSQL 所有权边界。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column

import app.db.plugin.locator as locator_module
import app.db.plugin.migration as migration_module
import app.db.plugin.registry as registry_module
from app.db.base import Base as HostBase
from app.db.plugin.base import plugin_declarative_base
from app.db.plugin.locator import plugin_schema_name, sqlite_sidecar_paths


@pytest.fixture(autouse=True)
def _isolate_plugin_databases():
    """快照插件数据库句柄注册表，用例结束后释放残留句柄并还原快照。"""
    snapshot = dict(registry_module._handles)
    registry_module._handles.clear()
    yield
    for plugin_id in list(registry_module._handles):
        registry_module.release_database(plugin_id)
    registry_module._handles.clear()
    registry_module._handles.update(snapshot)


@pytest.fixture
def plugin_data_root(tmp_path, monkeypatch) -> Path:
    """把插件数据库文件隔离到进程私有的临时目录。"""
    root = tmp_path / "plugins"
    monkeypatch.setattr(
        locator_module,
        "get_runtime_setting",
        lambda key, default=None: root if key == "PLUGIN_DATA_PATH" else default,
    )
    return root


@pytest.fixture
def sqlite_backend(monkeypatch):
    """把宿主数据库类型固定为 SQLite。"""
    monkeypatch.setattr(
        registry_module,
        "get_runtime_setting",
        lambda key, default=None: "sqlite" if key == "DB_TYPE" else default,
    )


@pytest.fixture
def postgresql_backend(monkeypatch):
    """把宿主数据库类型固定为 PostgreSQL，并用替身覆盖宿主引擎。"""
    host_engine = MagicMock(name="host_engine")
    derived_engine = MagicMock(name="derived_engine")
    host_engine.execution_options.return_value = derived_engine
    monkeypatch.setattr(
        registry_module,
        "get_runtime_setting",
        lambda key, default=None: "postgresql" if key == "DB_TYPE" else default,
    )
    monkeypatch.setattr(registry_module, "get_engine", lambda: host_engine)
    return host_engine, derived_engine


def test_plugin_declarative_base_returns_a_fresh_metadata_per_call():
    """两次调用互不共享 MetaData，两个基类上都能定义同名表而不抛错。"""
    base_a = plugin_declarative_base()
    base_b = plugin_declarative_base()
    assert base_a.metadata is not base_b.metadata

    class ItemA(base_a):
        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)

    class ItemB(base_b):
        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)

    assert "items" in base_a.metadata.tables
    assert "items" in base_b.metadata.tables


def test_declared_models_create_tables_in_the_plugin_own_database(plugin_data_root, sqlite_backend):
    """按声明的模型建表，数据可插入读回，且不污染宿主 Base.metadata。"""
    base = plugin_declarative_base()

    class Widget(base):
        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column()

    registry_module.ensure_database("demo", (Widget,))
    handle = registry_module.get_database("demo")
    session = handle.session()
    try:
        session.add(Widget(id=1, name="a"))
        session.commit()
        assert session.query(Widget).count() == 1
    finally:
        session.close()

    assert "widgets" not in HostBase.metadata.tables


def test_two_plugins_may_declare_the_same_table_name(plugin_data_root, sqlite_backend):
    """两个插件各自的同名表互不冲突，各自库文件不同，数据互不可见。"""
    base_a = plugin_declarative_base()
    base_b = plugin_declarative_base()

    class ItemA(base_a):
        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)
        label: Mapped[str] = mapped_column()

    class ItemB(base_b):
        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)
        note: Mapped[str] = mapped_column()

    registry_module.ensure_database("plugin_a", (ItemA,))
    registry_module.ensure_database("plugin_b", (ItemB,))
    handle_a = registry_module.get_database("plugin_a")
    handle_b = registry_module.get_database("plugin_b")
    assert handle_a.db_path != handle_b.db_path

    session_a = handle_a.session()
    session_b = handle_b.session()
    try:
        session_a.add(ItemA(id=1, label="x"))
        session_a.commit()
        session_b.add(ItemB(id=1, note="y"))
        session_b.commit()
        assert session_a.query(ItemA).count() == 1
        assert session_b.query(ItemB).count() == 1
    finally:
        session_a.close()
        session_b.close()


def test_only_declared_tables_are_created(plugin_data_root, sqlite_backend):
    """只建声明的表，未声明的同基类模型不会被创建。"""
    base = plugin_declarative_base()

    class Declared(base):
        __tablename__ = "declared"
        id: Mapped[int] = mapped_column(primary_key=True)

    class Undeclared(base):
        __tablename__ = "undeclared"
        id: Mapped[int] = mapped_column(primary_key=True)

    registry_module.ensure_database("demo", (Declared,))
    handle = registry_module.get_database("demo")
    tables = set(sa_inspect(handle.engine).get_table_names())
    assert tables == {"declared"}


def test_sqlite_database_lands_in_the_plugin_data_directory(plugin_data_root, sqlite_backend):
    """SQLite 库文件落在插件数据目录下，句柄独占引擎，不带 schema。"""
    handle = registry_module.get_database("demo")
    assert handle.db_path == plugin_data_root / "demo" / "plugin.db"
    assert handle.owns_engine is True
    assert handle.schema is None
    assert handle.db_path.exists()


def test_undeclared_plugin_creates_no_database_file(plugin_data_root, sqlite_backend):
    """两项声明都为空时不建句柄、不落盘。"""
    registry_module.ensure_database("demo")
    assert "demo" not in registry_module._handles
    assert not (plugin_data_root / "demo").exists()


def test_release_disposes_the_owned_sqlite_engine(plugin_data_root, sqlite_backend, monkeypatch):
    """release 只 dispose 句柄独占的引擎，并把句柄移出注册表。"""
    handle = registry_module.get_database("demo")
    calls: list[str] = []
    monkeypatch.setattr(handle.engine, "dispose", lambda: calls.append("disposed"))
    registry_module.release_database("demo")
    assert calls == ["disposed"]
    assert "demo" not in registry_module._handles


def test_release_all_disposes_every_plugin_database(plugin_data_root, sqlite_backend, monkeypatch):
    """release_all 释放全部插件的数据库连接，注册表清空。"""
    handle_a = registry_module.get_database("plugin_a")
    handle_b = registry_module.get_database("plugin_b")
    calls: list[str] = []
    monkeypatch.setattr(handle_a.engine, "dispose", lambda: calls.append("a"))
    monkeypatch.setattr(handle_b.engine, "dispose", lambda: calls.append("b"))
    registry_module.release_all_databases()
    assert set(calls) == {"a", "b"}
    assert registry_module._handles == {}


def test_destroy_removes_the_database_file_and_sidecars(plugin_data_root, sqlite_backend):
    """destroy 删除库文件及其 -wal/-shm 边车文件，并移出注册表。"""
    handle = registry_module.get_database("demo")
    for sidecar in sqlite_sidecar_paths(handle.db_path):
        sidecar.write_text("")
    registry_module.destroy_database("demo")
    assert not handle.db_path.exists()
    for sidecar in sqlite_sidecar_paths(handle.db_path):
        assert not sidecar.exists()
    assert "demo" not in registry_module._handles


def test_destroy_after_release_still_removes_the_database_file(plugin_data_root, sqlite_backend):
    """先 release 再 destroy 时库文件仍会被删除，覆盖 reset 流程的真实调用顺序。"""
    handle = registry_module.get_database("demo")
    db_path = handle.db_path
    registry_module.release_database("demo")
    assert db_path.exists()
    registry_module.destroy_database("demo")
    assert not db_path.exists()


def test_destroy_never_raises_when_the_file_cannot_be_removed(plugin_data_root, sqlite_backend, monkeypatch):
    """删除失败只记日志，不得向上抛出异常。"""
    registry_module.get_database("demo")

    def _raise_unlink(self, missing_ok=False):
        """模拟文件系统拒绝删除。"""
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)
    registry_module.destroy_database("demo")


def test_postgresql_handle_does_not_own_the_host_engine(postgresql_backend):
    """PostgreSQL 下句柄只是宿主引擎按 schema 派生的外观，不拥有它。"""
    host_engine, derived_engine = postgresql_backend
    handle = registry_module.get_database("demo")
    assert handle.owns_engine is False
    assert handle.engine is derived_engine
    assert handle.db_path is None
    host_engine.execution_options.assert_called_once_with(
        schema_translate_map={None: handle.schema}
    )


def test_postgresql_release_never_disposes_the_host_engine(postgresql_backend):
    """release 在 PostgreSQL 下不得 dispose 派生引擎，也不得触碰宿主引擎。"""
    host_engine, derived_engine = postgresql_backend
    registry_module.get_database("demo")
    registry_module.release_database("demo")
    derived_engine.dispose.assert_not_called()
    host_engine.dispose.assert_not_called()


def test_postgresql_destroy_drops_the_schema_and_keeps_the_host_engine(postgresql_backend):
    """destroy 在 PostgreSQL 下丢弃对应 schema，且不 dispose 任何引擎。"""
    host_engine, derived_engine = postgresql_backend
    handle = registry_module.get_database("demo")
    schema = handle.schema
    registry_module.destroy_database("demo")
    connection = host_engine.begin.return_value.__enter__.return_value
    executed = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert any("DROP SCHEMA" in sql and schema in sql for sql in executed)
    derived_engine.dispose.assert_not_called()
    host_engine.dispose.assert_not_called()


def test_declared_migrations_take_precedence_over_models(plugin_data_root, sqlite_backend, monkeypatch):
    """同时声明模型与迁移目录时优先走 alembic，且模型表不会被建出。"""
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        migration_module,
        "run_migrations",
        lambda handle, directory: calls.append((handle.plugin_id, directory)),
    )
    base = plugin_declarative_base()

    class Widget(base):
        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)

    registry_module.ensure_database("demo", (Widget,), Path("migrations"))
    assert calls == [("demo", Path("migrations"))]
    handle = registry_module.get_database("demo")
    assert "widgets" not in sa_inspect(handle.engine).get_table_names()


def test_plugin_schema_name_sanitizes_the_plugin_id():
    """schema 名只保留小写字母、数字与下划线。"""
    assert plugin_schema_name("Demo-Plugin.v2") == "plugin_demo_plugin_v2"


def test_sqlite_sidecar_paths_cover_wal_and_shm():
    """边车路径覆盖 -wal 与 -shm 两个后缀。"""
    db_path = Path("/tmp/plugin.db")
    assert sqlite_sidecar_paths(db_path) == (
        Path("/tmp/plugin.db-wal"),
        Path("/tmp/plugin.db-shm"),
    )
