"""
服务实例配置表建表迁移的结构不变量。

SQLite 不支持事后 ALTER TABLE 添加约束，唯一约束必须在 CREATE TABLE 时一次带上；条件
唯一索引的谓词是方言特性，丢掉谓词会把「每族至多一个默认调用目标」退化成「每族只能有
一行配置」。两者都只能靠真跑一遍迁移再回读结构来证明。

迁移建出的结构还必须与模型收敛：老库走 alembic、全新安装走 create_all，两条路落到同一
个表上，否则约束只在其中一条路上生效。
"""
import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.serviceconfig import ServiceConfig


MIGRATION = "database.versions.f8767f021120_3_0_8"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _index_ddl(connection, index_name: str) -> str:
    """回读 SQLite 中该索引的建表语句。"""
    return connection.exec_driver_sql(
        f"SELECT sql FROM sqlite_master WHERE type='index' AND name='{index_name}'"
    ).scalar()


def test_serviceconfig_migration_creates_unique_constraint_and_indexes(monkeypatch) -> None:
    """空库应可重复升级建表，唯一约束与两条索引都必须真实存在。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "serviceconfig" in inspector.get_table_names()

        uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("serviceconfig")
        }
        assert uniques["ux_serviceconfig_capability_type_name"] == (
            "capability", "type", "name",
        )

        indexes = {
            index["name"]: (tuple(index["column_names"]), index["unique"])
            for index in inspector.get_indexes("serviceconfig")
        }
        assert indexes["ix_serviceconfig_provider"] == (("provider",), 0)
        assert indexes["ux_serviceconfig_default_target"] == (("capability",), 1)

        migration.downgrade()
        assert "serviceconfig" not in sa.inspect(connection).get_table_names()


def test_serviceconfig_default_target_index_keeps_its_predicate(monkeypatch) -> None:
    """默认调用目标索引必须是条件索引，谓词丢失会把整族锁成只能有一行配置。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        ddl = _index_ddl(connection, "ux_serviceconfig_default_target")
        assert "WHERE is_default_target IS 1" in ddl


def test_serviceconfig_migration_converges_to_model_columns(monkeypatch) -> None:
    """迁移建出的列必须与模型完全一致，两条安装路径才会落到同一个结构上。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        columns = {
            column["name"] for column in sa.inspect(connection).get_columns("serviceconfig")
        }
        assert columns == {column.name for column in ServiceConfig.__table__.columns}


def test_serviceconfig_migration_enforces_constraints_on_real_rows(monkeypatch) -> None:
    """
    迁移建出的库上真写几行，验证两条约束由数据库判定而非模型。

    模型侧的 ``__table_args__`` 只覆盖 create_all 路径；老库升级走的是迁移建出的表，
    约束若只写在模型里，这条路上就一条也拦不住。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        def _insert(service_type: str, name: str, provider: str, default: int) -> None:
            connection.exec_driver_sql(
                "INSERT INTO serviceconfig "
                "(capability, type, name, enabled, config, is_default_target, provider) "
                "VALUES ('downloader', ?, ?, 0, NULL, ?, ?)",
                (service_type, name, default, provider),
            )

        _insert("qbittorrent", "alpha", "host:builtin", 1)

        # 同名配置跨 provider 同样被拦
        try:
            _insert("qbittorrent", "alpha", "SomePlugin", 0)
            raise AssertionError("唯一约束未跨 provider 生效")
        except sa.exc.IntegrityError:
            pass

        # 同族第二个默认调用目标被条件唯一索引拦下
        try:
            _insert("transmission", "beta", "host:builtin", 1)
            raise AssertionError("条件唯一索引未拦下第二个默认调用目标")
        except sa.exc.IntegrityError:
            pass
