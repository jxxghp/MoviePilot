"""插件自管理数据库框架：建表隔离、SQLite 生命周期与 PostgreSQL 所有权边界。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column, scoped_session, sessionmaker

import app.db.engine as engine_module
import app.db.plugin.locator as locator_module
import app.db.plugin.migration as migration_module
import app.db.plugin.registry as registry_module
import app.db.session as session_module
from app.db.base import Base as HostBase
from app.db.plugin.base import plugin_declarative_base
from app.db.plugin.container import PluginDatabaseHandle
from app.db.plugin.locator import (
    SCHEMA_NAME_MAX_LENGTH,
    plugin_schema_name,
    sqlite_sidecar_paths,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_databases():
    """快照插件数据库句柄，用例结束后释放残留句柄并还原快照。"""
    handles = dict(registry_module._handles)
    registry_module._handles.clear()
    yield
    for plugin_id in list(registry_module._handles):
        registry_module.release_database(plugin_id)
    registry_module._handles.clear()
    registry_module._handles.update(handles)


def _raise_dispose() -> None:
    """模拟连接池释放失败。"""
    raise RuntimeError("dispose failed")


def _borrowed_engine_handle(engine: Any) -> PluginDatabaseHandle:
    """
    构造一个不拥有引擎的句柄，用于验证 PostgreSQL 分支上的连接路由。
    :param engine: 句柄借用的引擎
    :return: owns_engine 为假的数据库句柄
    """
    session_factory = sessionmaker(bind=engine)
    return PluginDatabaseHandle(
        plugin_id="demo",
        engine=engine,
        session_factory=session_factory,
        scoped_session_factory=scoped_session(session_factory),
        db_path=None,
        schema="plugin_demo",
        owns_engine=False,
    )


def _write_migration_directory(root: Path) -> Path:
    """
    写出一个最小可用的 Alembic script_location，只含一条建表迁移。
    :param root: 承载迁移目录的父目录
    :return: 迁移目录路径
    """
    directory = root / "migrations"
    (directory / "versions").mkdir(parents=True)
    (directory / "env.py").write_text(
        """from alembic import context
from sqlalchemy import create_engine


def _run(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


injected = context.config.attributes.get("connection")
if injected is not None:
    _run(injected)
else:
    engine = create_engine(context.config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()
""",
        encoding="utf-8",
    )
    (directory / "versions" / "0001_create_notes.py").write_text(
        '''"""建立插件自有表。"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """建出插件声明的表。"""
    op.create_table("notes", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade():
    """回退建表。"""
    op.drop_table("notes")
''',
        encoding="utf-8",
    )
    return directory


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
    # 派生引擎必须是真实引擎：注册 begin 监听器要求一个真实的 SQLAlchemy 事件目标，
    # 替身对象会被 event.listen 拒绝
    borrowed_engine = create_engine("sqlite://")
    derived_engine = borrowed_engine.execution_options()
    host_engine.execution_options.return_value = derived_engine
    monkeypatch.setattr(
        registry_module,
        "get_runtime_setting",
        lambda key, default=None: "postgresql" if key == "DB_TYPE" else default,
    )
    monkeypatch.setattr(registry_module, "get_engine", lambda: host_engine)
    yield host_engine, derived_engine
    borrowed_engine.dispose()


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


def test_postgresql_release_never_disposes_the_host_engine(postgresql_backend, monkeypatch):
    """release 在 PostgreSQL 下不得 dispose 派生引擎，也不得触碰宿主引擎。"""
    host_engine, derived_engine = postgresql_backend
    disposed: list[str] = []
    monkeypatch.setattr(derived_engine, "dispose", lambda: disposed.append("derived"))
    registry_module.get_database("demo")
    registry_module.release_database("demo")
    assert disposed == []
    host_engine.dispose.assert_not_called()


def test_postgresql_destroy_drops_the_schema_and_keeps_the_host_engine(
    postgresql_backend,
    monkeypatch,
):
    """destroy 在 PostgreSQL 下丢弃对应 schema，且不 dispose 任何引擎。"""
    host_engine, derived_engine = postgresql_backend
    disposed: list[str] = []
    monkeypatch.setattr(derived_engine, "dispose", lambda: disposed.append("derived"))
    handle = registry_module.get_database("demo")
    schema = handle.schema
    registry_module.destroy_database("demo")
    connection = host_engine.begin.return_value.__enter__.return_value
    executed = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert any("DROP SCHEMA" in sql and schema in sql for sql in executed)
    assert disposed == []
    host_engine.dispose.assert_not_called()


def test_declared_migrations_take_precedence_over_models(
    plugin_data_root,
    sqlite_backend,
    monkeypatch,
    tmp_path,
):
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

    directory = _write_migration_directory(tmp_path)
    registry_module.ensure_database("demo", (Widget,), directory)
    assert calls == [("demo", directory)]
    handle = registry_module.get_database("demo")
    assert "widgets" not in sa_inspect(handle.engine).get_table_names()


def test_plugin_schema_name_sanitizes_the_plugin_id():
    """schema 名只保留小写字母、数字与下划线，被改写过的标识再带上区分哈希。"""
    schema = plugin_schema_name("Demo-Plugin.v2")
    assert schema.startswith("plugin_demo_plugin_v2_")
    assert schema.removeprefix("plugin_demo_plugin_v2_").isalnum()


def test_plugin_schema_name_keeps_an_already_legal_plugin_id_verbatim():
    """标识本身已是合法 schema 片段时不追加哈希。"""
    assert plugin_schema_name("my_plugin") == "plugin_my_plugin"


def test_plugin_schema_name_separates_ids_that_normalize_to_the_same_text():
    """归一后同名的三个插件标识各自拿到不同 schema，卸载互不波及。"""
    schemas = {
        plugin_schema_name(plugin_id)
        for plugin_id in ("My-Plugin", "My_Plugin", "my_plugin")
    }
    assert len(schemas) == 3


def test_plugin_schema_name_fits_the_postgresql_identifier_limit():
    """超长插件标识被截断到 PostgreSQL 标识符上限以内，且仍带区分哈希。"""
    schema = plugin_schema_name("p" * 200)
    assert len(schema.encode("utf-8")) <= SCHEMA_NAME_MAX_LENGTH
    assert plugin_schema_name("p" * 200) != plugin_schema_name("p" * 201)


def test_sqlite_sidecar_paths_cover_wal_and_shm():
    """边车路径覆盖 -wal 与 -shm 两个后缀。"""
    db_path = Path("/tmp/plugin.db")
    assert sqlite_sidecar_paths(db_path) == (
        Path("/tmp/plugin.db-wal"),
        Path("/tmp/plugin.db-shm"),
    )


def test_release_all_isolates_a_failing_plugin_dispose(plugin_data_root, sqlite_backend, monkeypatch):
    """一个插件的连接池释放抛错，其余插件仍被释放，异常不向上传播。"""
    handle_a = registry_module.get_database("plugin_a")
    handle_b = registry_module.get_database("plugin_b")
    calls: list[str] = []
    monkeypatch.setattr(handle_a.engine, "dispose", _raise_dispose)
    monkeypatch.setattr(handle_b.engine, "dispose", lambda: calls.append("b"))

    registry_module.release_all_databases()

    assert calls == ["b"]
    assert registry_module._handles == {}


def test_close_database_disposes_the_host_engine_when_a_plugin_dispose_fails(
    plugin_data_root,
    sqlite_backend,
    monkeypatch,
):
    """插件连接池释放抛错时，宿主同步引擎仍然被释放。"""
    handle = registry_module.get_database("demo")
    monkeypatch.setattr(handle.engine, "dispose", _raise_dispose)
    disposed: list[str] = []
    host_engine = MagicMock(name="host_sync_engine")
    host_engine.dispose.side_effect = lambda: disposed.append("host")
    monkeypatch.setattr(engine_module, "peek_sync_engine", lambda: host_engine)
    monkeypatch.setattr(engine_module, "peek_async_engine", lambda: None)
    monkeypatch.setattr(session_module, "_pooled_async_engines", {})

    asyncio.run(session_module.close_database())

    assert disposed == ["host"]


def test_destroy_removes_the_database_file_even_when_dispose_fails(
    plugin_data_root,
    sqlite_backend,
    monkeypatch,
):
    """连接池释放抛错不得中断销毁，库文件照样被删除。"""
    handle = registry_module.get_database("demo")
    monkeypatch.setattr(handle.engine, "dispose", _raise_dispose)

    registry_module.destroy_database("demo")

    assert not handle.db_path.exists()
    assert "demo" not in registry_module._handles


def test_single_table_inheritance_creates_the_shared_table_once(plugin_data_root, sqlite_backend):
    """单表继承的父子类共享同一张表，一并声明只建一次，重复建库幂等。"""
    base = plugin_declarative_base()

    class Node(base):
        __tablename__ = "nodes"
        __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "node"}
        id: Mapped[int] = mapped_column(primary_key=True)
        kind: Mapped[str] = mapped_column()

    class Leaf(Node):
        __mapper_args__ = {"polymorphic_identity": "leaf"}

    assert Leaf.__table__ is Node.__table__

    registry_module.ensure_database("demo", (Node, Leaf))
    registry_module.ensure_database("demo", (Node, Leaf))

    handle = registry_module.get_database("demo")
    assert set(sa_inspect(handle.engine).get_table_names()) == {"nodes"}


def test_release_closes_the_thread_local_session(plugin_data_root, sqlite_backend):
    """release 先清掉线程局部会话，句柄不再扣着已 dispose 引擎上的连接。"""
    handle = registry_module.get_database("demo")
    session = handle.scoped_session()
    session.execute(text("SELECT 1"))
    assert handle.scoped_session_factory.registry.has() is True

    registry_module.release_database("demo")

    assert handle.scoped_session_factory.registry.has() is False


def test_relative_migrations_directory_is_rejected_before_any_file_is_created(
    plugin_data_root,
    sqlite_backend,
):
    """迁移目录是相对路径时直接抛错：它按宿主工作目录解析，可能是另一条迁移链。"""
    with pytest.raises(ValueError):
        registry_module.ensure_database("demo", (), Path("migrations"))

    assert "demo" not in registry_module._handles
    assert not (plugin_data_root / "demo").exists()


def test_missing_migrations_directory_is_rejected_before_any_file_is_created(
    plugin_data_root,
    sqlite_backend,
    tmp_path,
):
    """迁移目录不存在时直接抛错，不留下空库文件，也不建插件数据目录。"""
    with pytest.raises(FileNotFoundError):
        registry_module.ensure_database("demo", (), tmp_path / "missing")

    assert "demo" not in registry_module._handles
    assert not (plugin_data_root / "demo").exists()


def test_postgresql_handle_creates_the_plugin_schema(postgresql_backend):
    """PostgreSQL 下建句柄先按插件 schema 执行 CREATE SCHEMA IF NOT EXISTS。"""
    host_engine, _ = postgresql_backend
    handle = registry_module.get_database("demo")
    connection = host_engine.begin.return_value.__enter__.return_value
    executed = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert f'CREATE SCHEMA IF NOT EXISTS "{handle.schema}"' in executed


def test_concurrent_get_database_builds_a_single_handle(plugin_data_root, sqlite_backend):
    """八个线程同时取同一插件的句柄时只建出一个句柄，不会并存两份连接池。"""
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    guard = threading.Lock()
    handles: list[PluginDatabaseHandle] = []

    def _acquire() -> None:
        """在同一时刻取句柄并记录结果。"""
        barrier.wait()
        handle = registry_module.get_database("demo")
        with guard:
            handles.append(handle)

    threads = [threading.Thread(target=_acquire) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(handles) == thread_count
    assert all(handle is handles[0] for handle in handles)
    assert list(registry_module._handles) == ["demo"]


def test_get_database_rebuilds_a_released_handle(plugin_data_root, sqlite_backend):
    """release 之后再取句柄会重建出一个可用的新句柄。"""
    first = registry_module.get_database("demo")
    registry_module.release_database("demo")

    second = registry_module.get_database("demo")

    assert second is not first
    session = second.session()
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()


def test_get_database_rebuilds_a_destroyed_handle(plugin_data_root, sqlite_backend):
    """destroy 之后再取句柄会重建库文件与新句柄。"""
    first = registry_module.get_database("demo")
    registry_module.destroy_database("demo")

    second = registry_module.get_database("demo")

    assert second is not first
    assert second.db_path.exists()
    session = second.session()
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()


def test_run_migrations_upgrades_a_sqlite_plugin_database(plugin_data_root, sqlite_backend, tmp_path):
    """声明迁移目录时按 alembic 建表并写入版本号，重复建库幂等。"""
    directory = _write_migration_directory(tmp_path)

    registry_module.ensure_database("demo", (), directory)

    handle = registry_module.get_database("demo")
    tables = set(sa_inspect(handle.engine).get_table_names())
    assert {"notes", "alembic_version"} <= tables

    registry_module.ensure_database("demo", (), directory)

    assert set(sa_inspect(handle.engine).get_table_names()) == tables


def test_run_migrations_routes_the_postgresql_connection_through_the_handle(
    monkeypatch,
    tmp_path,
):
    """PostgreSQL 下迁移复用句柄已限定 schema 的连接，并在结束后提交。"""
    captured: dict[str, object] = {}

    def _record_upgrade(config, revision):
        """记录 alembic 收到的连接与目标版本。"""
        captured["connection"] = config.attributes.get("connection")
        captured["revision"] = revision

    monkeypatch.setattr(migration_module, "upgrade", _record_upgrade)
    handle = _borrowed_engine_handle(MagicMock(name="derived_engine"))

    migration_module.run_migrations(handle, _write_migration_directory(tmp_path))

    connection = handle.engine.connect.return_value.__enter__.return_value
    assert captured["connection"] is connection
    assert captured["revision"] == "head"
    connection.commit.assert_called_once()


def test_release_after_destroy_keeps_a_database_rebuilt_by_a_stop_hook(
    plugin_data_root,
    sqlite_backend,
):
    """销毁后重新建出的库属于仍在运行的插件，普通停止只释放连接、不得再删一次。"""
    registry_module.get_database("demo")
    registry_module.destroy_database("demo")

    rebuilt = registry_module.get_database("demo")
    session = rebuilt.session()
    try:
        session.execute(text("CREATE TABLE kept (id INTEGER PRIMARY KEY)"))
        session.commit()
    finally:
        session.close()

    registry_module.release_database("demo")

    assert rebuilt.db_path.exists()
    assert "demo" not in registry_module._handles


def test_destroy_blocks_a_concurrent_handle_rebuild_until_the_carrier_is_removed(
    plugin_data_root,
    sqlite_backend,
    monkeypatch,
):
    """销毁未删完载体前并发的取句柄取不到结果，新句柄因此不会指向被删掉的载体。"""
    first = registry_module.get_database("demo")
    entered_removal = threading.Event()
    resume_removal = threading.Event()
    original_remove_storage = registry_module._remove_storage

    def _blocking_remove_storage(plugin_id, handle):
        """在删除载体前挂住销毁流程，制造并发窗口。"""
        entered_removal.set()
        assert resume_removal.wait(10)
        original_remove_storage(plugin_id, handle)

    monkeypatch.setattr(registry_module, "_remove_storage", _blocking_remove_storage)
    rebuilt: list[PluginDatabaseHandle] = []

    def _rebuild() -> None:
        """在销毁进行中重新取句柄。"""
        rebuilt.append(registry_module.get_database("demo"))

    destroyer = threading.Thread(target=registry_module.destroy_database, args=("demo",))
    destroyer.start()
    assert entered_removal.wait(10)
    rebuilder = threading.Thread(target=_rebuild)
    rebuilder.start()
    rebuilder.join(0.5)

    assert rebuilder.is_alive()
    assert rebuilt == []

    resume_removal.set()
    destroyer.join(10)
    rebuilder.join(10)

    assert not rebuilder.is_alive()
    assert rebuilt and rebuilt[0] is not first
    assert rebuilt[0].db_path.exists()


def test_search_path_setter_binds_the_quoted_plugin_schema():
    """监听器在事务开始时执行 SET LOCAL search_path，schema 名带引号且不留 public 兜底。"""
    connection = MagicMock(name="connection")

    registry_module._search_path_setter("plugin_demo")(connection)

    executed = connection.exec_driver_sql.call_args.args[0]
    assert executed == 'SET LOCAL search_path TO "plugin_demo"'
    assert "public" not in executed


def test_begin_listener_on_a_derived_engine_never_reaches_the_host_engine(tmp_path):
    """派生引擎上的 begin 监听器只对派生连接生效，宿主引擎的连接不触发。"""
    host_engine = create_engine(f"sqlite:///{tmp_path / 'host.db'}")
    derived_engine = host_engine.execution_options()
    fired: list[str] = []

    def _listener(connection) -> None:
        """记录触发并在同一连接上执行一条无害语句，验证不会递归或报错。"""
        fired.append("begin")
        connection.exec_driver_sql("SELECT 1")

    event.listen(derived_engine, "begin", _listener)

    with derived_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        connection.commit()
    assert fired == ["begin"]

    with host_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        connection.commit()
    assert fired == ["begin"]

    session = sessionmaker(bind=derived_engine)()
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
        session.commit()
    finally:
        session.close()
    assert fired == ["begin", "begin"]

    host_engine.dispose()


def test_postgresql_handle_binds_a_search_path_listener_to_the_derived_engine(
    postgresql_backend,
    monkeypatch,
):
    """PostgreSQL 建句柄时把插件 schema 的监听器挂到派生引擎，而不是宿主引擎。"""
    _host_engine, derived_engine = postgresql_backend
    bound: list[tuple] = []
    build_listener = registry_module._search_path_setter

    def _record(schema: str):
        """记录被绑定的 schema 与生成的监听器。"""
        listener = build_listener(schema)
        bound.append((schema, listener))
        return listener

    monkeypatch.setattr(registry_module, "_search_path_setter", _record)

    handle = registry_module.get_database("demo")

    assert [schema for schema, _ in bound] == [handle.schema]
    assert event.contains(derived_engine, "begin", bound[0][1])


def test_alembic_migrations_trigger_the_begin_event_on_a_borrowed_engine(tmp_path):
    """借用引擎的迁移在连接上首次执行即触发 begin，search_path 监听器因此覆盖 alembic。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'host.db'}").execution_options()
    begins: list[str] = []
    event.listen(engine, "begin", lambda _connection: begins.append("begin"))
    handle = _borrowed_engine_handle(engine)

    migration_module.run_migrations(handle, _write_migration_directory(tmp_path))

    assert begins
    assert "notes" in set(sa_inspect(engine).get_table_names())

    engine.dispose()
