"""插件自管理数据库框架测试：建表隔离、SQLite 生命周期与 PostgreSQL 所有权边界。"""

from typing import Iterator, Optional
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Mapped, mapped_column

from app.db.plugin import registry as plugin_registry_module
from app.db.plugin.base import plugin_declarative_base
from app.db.plugin.registry import (
    declare_models,
    destroy_database,
    ensure_database,
    get_database,
    release_instance,
)
from app.plugins import plugin_instance_path
from app.runtime.config import settings
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


@pytest.fixture(autouse=True)
def _isolate_plugin_database_registry() -> Iterator[None]:
    """
    快照并复原插件数据库注册表的全局状态。

    容器与声明都是模块级字典，用例之间若不隔离，前一个用例建立的容器会被后一个
    用例复用，两者互相污染。
    """
    saved_declarations = dict(plugin_registry_module._declarations)
    saved_containers = dict(plugin_registry_module._containers)
    plugin_registry_module._declarations.clear()
    plugin_registry_module._containers.clear()
    yield
    for key in list(plugin_registry_module._containers):
        plugin_registry_module.release_instance(*key)
    plugin_registry_module._declarations.clear()
    plugin_registry_module._declarations.update(saved_declarations)
    plugin_registry_module._containers.clear()
    plugin_registry_module._containers.update(saved_containers)


# --------------------------------------------------------------------------- #
# 建表隔离
# --------------------------------------------------------------------------- #


def test_declared_models_build_tables_in_plugin_own_database():
    """插件声明模型后建表成功，表落在插件自己的库里，且宿主 Base 看不见它。"""
    plugin_id = "DbFrameworkPluginOne"
    base = plugin_declarative_base(plugin_id)

    class Widget(base):
        """测试用插件模型。"""

        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[Optional[str]] = mapped_column(default=None)

    declare_models(plugin_id, DEFAULT_INSTANCE_ID, base)
    ensure_database(plugin_id, DEFAULT_INSTANCE_ID)

    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)
    with handle.session() as session:
        session.add(Widget(name="a"))
        session.commit()
        stored = session.query(Widget).first()
        assert stored.name == "a"

    # 宿主的建表流程走独立的 app.db.base.Base.metadata，看不到插件表
    from app.db.base import Base as HostBase

    assert "widgets" not in HostBase.metadata.tables


def test_two_plugins_can_declare_the_same_table_name_without_conflict():
    """两个插件各自的声明式基类持有独立 MetaData，同名表互不冲突。"""
    plugin_a, plugin_b = "DbFrameworkPluginTwoA", "DbFrameworkPluginTwoB"
    base_a = plugin_declarative_base(plugin_a)
    base_b = plugin_declarative_base(plugin_b)

    class ItemA(base_a):
        """插件 A 的同名表模型。"""

        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)

    class ItemB(base_b):
        """插件 B 的同名表模型，字段结构与插件 A 不同。"""

        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)
        label: Mapped[Optional[str]] = mapped_column(default=None)

    declare_models(plugin_a, DEFAULT_INSTANCE_ID, base_a)
    declare_models(plugin_b, DEFAULT_INSTANCE_ID, base_b)
    ensure_database(plugin_a, DEFAULT_INSTANCE_ID)
    ensure_database(plugin_b, DEFAULT_INSTANCE_ID)

    handle_a = get_database(plugin_a, DEFAULT_INSTANCE_ID)
    handle_b = get_database(plugin_b, DEFAULT_INSTANCE_ID)
    assert handle_a.db_path != handle_b.db_path

    with handle_a.session() as session:
        session.add(ItemA(id=1))
        session.commit()
    with handle_b.session() as session:
        session.add(ItemB(id=1, label="x"))
        session.commit()
        assert session.get(ItemB, 1).label == "x"


# --------------------------------------------------------------------------- #
# SQLite 生命周期
# --------------------------------------------------------------------------- #


def test_sqlite_path_is_resolved_through_plugin_instance_path():
    """SQLite 库文件路径经 plugin_instance_path 取得，落在插件自身的 db 目录下。"""
    plugin_id = "DbFrameworkPluginThree"
    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)

    expected_dir = plugin_instance_path(plugin_id, DEFAULT_INSTANCE_ID, "db")
    assert handle.db_path == expected_dir / "plugin.db"
    assert handle.owns_engine is True
    assert handle.schema is None
    assert handle.db_path.exists()


def test_release_instance_disposes_owned_sqlite_engine(monkeypatch):
    """释放 SQLite 容器必须 dispose 它独占的连接池。"""
    plugin_id = "DbFrameworkPluginFour"
    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)
    disposed = []
    monkeypatch.setattr(handle.engine, "dispose", lambda: disposed.append(1))

    release_instance(plugin_id, DEFAULT_INSTANCE_ID)

    assert disposed == [1]
    # 释放后容器从注册表摘除，下一次取用会重新建立
    assert (plugin_id, DEFAULT_INSTANCE_ID) not in plugin_registry_module._containers


def test_destroy_database_removes_file_and_wal_shm_sidecars():
    """销毁 SQLite 库文件时必须一并清理 -wal/-shm 边车文件。"""
    plugin_id = "DbFrameworkPluginFive"
    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)
    wal = handle.db_path.with_name(handle.db_path.name + "-wal")
    shm = handle.db_path.with_name(handle.db_path.name + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"")
    assert handle.db_path.exists() and wal.exists() and shm.exists()

    destroy_database(plugin_id, DEFAULT_INSTANCE_ID)

    assert not handle.db_path.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert (plugin_id, DEFAULT_INSTANCE_ID) not in plugin_registry_module._containers


# --------------------------------------------------------------------------- #
# PostgreSQL 所有权边界（替身，不连真实 PG）
# --------------------------------------------------------------------------- #


def test_postgresql_container_does_not_own_engine(monkeypatch):
    """PostgreSQL 下容器复用宿主引擎派生的外观，不拥有该 engine。"""
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    host_engine = MagicMock()
    connection_ctx = MagicMock()
    connection_ctx.__enter__ = MagicMock(return_value=MagicMock())
    connection_ctx.__exit__ = MagicMock(return_value=False)
    host_engine.begin.return_value = connection_ctx
    derived_engine = MagicMock()
    host_engine.execution_options.return_value = derived_engine
    monkeypatch.setattr(plugin_registry_module, "get_engine", lambda: host_engine)

    plugin_id = "DbFrameworkPluginSix"
    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)

    assert handle.owns_engine is False
    assert handle.engine is derived_engine
    assert handle.db_path is None
    host_engine.execution_options.assert_called_once_with(
        schema_translate_map={None: handle.schema}
    )


def test_postgresql_release_does_not_dispose_host_engine(monkeypatch):
    """
    释放 PostgreSQL 容器绝不能 dispose 宿主连接池。

    容器只是宿主引擎按 schema_translate_map 派生的外观；插件停止或移除时如果
    错误地 dispose 了它，会连累宿主自身乃至其它插件仍在使用的连接。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    host_engine = MagicMock()
    connection_ctx = MagicMock()
    connection_ctx.__enter__ = MagicMock(return_value=MagicMock())
    connection_ctx.__exit__ = MagicMock(return_value=False)
    host_engine.begin.return_value = connection_ctx
    derived_engine = MagicMock()
    host_engine.execution_options.return_value = derived_engine
    monkeypatch.setattr(plugin_registry_module, "get_engine", lambda: host_engine)

    plugin_id = "DbFrameworkPluginSeven"
    get_database(plugin_id, DEFAULT_INSTANCE_ID)

    release_instance(plugin_id, DEFAULT_INSTANCE_ID)

    derived_engine.dispose.assert_not_called()
    host_engine.dispose.assert_not_called()


def test_postgresql_destroy_drops_schema_not_host_engine(monkeypatch):
    """PostgreSQL 下销毁必须丢弃对应 schema，且同样不得 dispose 宿主引擎。"""
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    host_engine = MagicMock()
    connection_ctx = MagicMock()
    fake_connection = MagicMock()
    connection_ctx.__enter__ = MagicMock(return_value=fake_connection)
    connection_ctx.__exit__ = MagicMock(return_value=False)
    host_engine.begin.return_value = connection_ctx
    derived_engine = MagicMock()
    host_engine.execution_options.return_value = derived_engine
    monkeypatch.setattr(plugin_registry_module, "get_engine", lambda: host_engine)

    plugin_id = "DbFrameworkPluginEight"
    handle = get_database(plugin_id, DEFAULT_INSTANCE_ID)
    schema = handle.schema
    fake_connection.reset_mock()
    host_engine.begin.reset_mock()

    destroy_database(plugin_id, DEFAULT_INSTANCE_ID)

    derived_engine.dispose.assert_not_called()
    host_engine.dispose.assert_not_called()
    executed_sql = str(fake_connection.execute.call_args[0][0])
    assert schema in executed_sql
    assert "DROP SCHEMA" in executed_sql.upper()


# --------------------------------------------------------------------------- #
# 未声明时不建库
# --------------------------------------------------------------------------- #


def test_no_declaration_creates_no_database_file():
    """插件未声明任何模型或迁移目录时，ensure_database 不创建任何库文件。"""
    plugin_id = "DbFrameworkPluginNine"

    ensure_database(plugin_id, DEFAULT_INSTANCE_ID)

    assert (plugin_id, DEFAULT_INSTANCE_ID) not in plugin_registry_module._containers
    plugin_root = settings.PLUGIN_DATA_PATH / plugin_id
    assert not plugin_root.exists()
