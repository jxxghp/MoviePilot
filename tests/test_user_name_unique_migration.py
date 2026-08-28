"""用户名唯一性 Alembic 迁移测试。"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.db.engine import _register_sqlite_foreign_keys
from app.db.models.passkey import PassKey
from app.db.models.user import User
from app.db.models.userconfig import UserConfig

MIGRATION_MODULE = "database.versions.a9d4f2c7e6b1_3_0_18"


def _bind_migration(monkeypatch, connection):
    """把用户名迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _legacy_metadata() -> tuple[sa.MetaData, sa.Table, sa.Table]:
    """构造允许重名且带用户外键的旧版最小表结构。"""
    metadata = sa.MetaData()
    users = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Index("ix_user_name", "name"),
    )
    passkeys = sa.Table(
        "passkey",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            nullable=False,
        ),
    )
    return metadata, users, passkeys


def _user_rows(connection, users: sa.Table) -> list[dict]:
    """按稳定身份返回用户快照。"""
    return list(connection.execute(sa.select(users).order_by(users.c.id)).mappings())


def _indexes(connection) -> dict[str, dict]:
    """返回用户表的命名索引。"""
    return {index["name"]: index for index in sa.inspect(connection).get_indexes("user")}


def test_user_model_declares_exact_unique_name_index() -> None:
    """当前用户聚合模型必须声明唯一身份和精确级联约束。"""
    indexes = {index.name: index for index in User.__table__.indexes}

    assert "ix_user_name" not in indexes
    assert indexes["ux_user_name"].unique is True
    assert tuple(column.name for column in indexes["ux_user_name"].columns) == ("name",)
    user_config_fk = next(iter(UserConfig.__table__.foreign_keys))
    assert user_config_fk.target_fullname == "user.name"
    assert user_config_fk.onupdate == "CASCADE"
    assert user_config_fk.ondelete == "CASCADE"
    assert UserConfig.__table__.c.username.nullable is False
    assert UserConfig.__table__.c.key.nullable is False
    constraints = {constraint.name: constraint for constraint in UserConfig.__table__.constraints}
    assert {column.name for column in constraints["uq_userconfig_username_key"].columns} == {"username", "key"}
    passkey_fk = next(iter(PassKey.__table__.foreign_keys))
    assert passkey_fk.target_fullname == "user.id"
    assert passkey_fk.ondelete == "CASCADE"


def test_sqlite_connection_registration_enables_foreign_keys() -> None:
    """每条宿主 SQLite 连接都必须实际启用外键检查。"""
    engine = sa.create_engine("sqlite://")
    _register_sqlite_foreign_keys(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    engine.dispose()


def test_user_child_constraints_compile_for_postgresql() -> None:
    """用户从属约束必须生成 PostgreSQL 可执行的级联 DDL。"""
    user_config_ddl = str(
        CreateTable(UserConfig.__table__).compile(
            dialect=postgresql.dialect(),
        )
    )
    passkey_ddl = str(
        CreateTable(PassKey.__table__).compile(
            dialect=postgresql.dialect(),
        )
    )

    assert ('FOREIGN KEY(username) REFERENCES "user" (name) ON DELETE CASCADE ON UPDATE CASCADE') in user_config_ddl
    assert ('FOREIGN KEY(user_id) REFERENCES "user" (id) ON DELETE CASCADE') in passkey_ddl


def test_user_child_migration_repairs_data_and_cascades(monkeypatch) -> None:
    """迁移清债后配置和 PassKey 不得脱离用户主体，并保持可逆。"""
    metadata = sa.MetaData()
    users = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean()),
        sa.Index("ix_user_name", "name"),
    )
    configs = sa.Table(
        "userconfig",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("key", sa.String(), nullable=True),
        sa.Column("value", sa.JSON()),
    )
    passkeys = sa.Table(
        "passkey",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            nullable=False,
        ),
    )
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        metadata.create_all(connection)
        connection.execute(
            users.insert().values(
                id=1,
                name="old",
                is_active=True,
            )
        )
        connection.execute(
            configs.insert(),
            [
                {"id": 1, "username": "old", "key": "theme", "value": "first"},
                {"id": 2, "username": "old", "key": "theme", "value": "duplicate"},
                {"id": 3, "username": "ghost", "key": "theme", "value": "orphan"},
                {"id": 4, "username": None, "key": "theme", "value": "null-user"},
                {"id": 5, "username": "old", "key": None, "value": "null-key"},
            ],
        )
        connection.execute(
            passkeys.insert(),
            [
                {"id": 11, "user_id": 1},
                {"id": 12, "user_id": 999},
            ],
        )
        connection.commit()
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        connection.commit()
        migration.upgrade()
        connection.commit()

        assert connection.execute(sa.select(configs.c.id, configs.c.username, configs.c.key)).all() == [
            (1, "old", "theme")
        ]
        assert connection.execute(sa.select(passkeys.c.id)).scalars().all() == [11]
        inspector = sa.inspect(connection)
        config_columns = {column["name"]: column for column in inspector.get_columns("userconfig")}
        assert config_columns["username"]["nullable"] is False
        assert config_columns["key"]["nullable"] is False
        config_fk = inspector.get_foreign_keys("userconfig")[0]
        assert config_fk["options"] == {
            "ondelete": "CASCADE",
            "onupdate": "CASCADE",
        }
        passkey_fk = inspector.get_foreign_keys("passkey")[0]
        assert passkey_fk["options"] == {"ondelete": "CASCADE"}
        unique = {item["name"]: item for item in inspector.get_unique_constraints("userconfig")}
        assert unique["uq_userconfig_username_key"]["column_names"] == [
            "username",
            "key",
        ]

        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.execute(users.update().values(name="new"))
        assert connection.execute(sa.select(configs.c.username)).scalar_one() == "new"
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    configs.insert().values(
                        username="new",
                        key="theme",
                        value="duplicate",
                    )
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    configs.insert().values(
                        username="new",
                        key=None,
                        value="empty-key",
                    )
                )
        connection.execute(users.delete())
        assert connection.execute(sa.select(configs.c.id)).all() == []
        assert connection.execute(sa.select(passkeys.c.id)).all() == []
        connection.commit()

        migration.downgrade()
        connection.commit()
        downgraded = sa.inspect(connection)
        columns = {column["name"]: column for column in downgraded.get_columns("userconfig")}
        assert columns["username"]["nullable"] is True
        assert columns["key"]["nullable"] is True
        assert downgraded.get_foreign_keys("userconfig") == []
        assert downgraded.get_foreign_keys("passkey")[0]["options"] == {}
    engine.dispose()


def test_migration_preserves_rows_and_foreign_keys_across_replay(
    monkeypatch,
) -> None:
    """升级应确定性修复重名并在降级、再升级时保留用户身份。"""
    engine = sa.create_engine("sqlite://")
    metadata, users, passkeys = _legacy_metadata()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        metadata.create_all(connection)
        connection.execute(
            users.insert(),
            [
                {"id": 1, "name": "alice", "is_active": True},
                {"id": 2, "name": "alice", "is_active": True},
                {"id": 3, "name": "alice", "is_active": None},
                {
                    "id": 4,
                    "name": "alice__duplicate_2",
                    "is_active": True,
                },
                {"id": 5, "name": "bob", "is_active": True},
            ],
        )
        connection.execute(
            passkeys.insert(),
            [
                {"id": 11, "user_id": 2},
                {"id": 12, "user_id": 3},
            ],
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        upgraded_rows = _user_rows(connection, users)
        migration.upgrade()

        assert _user_rows(connection, users) == upgraded_rows
        assert upgraded_rows == [
            {"id": 1, "name": "alice", "is_active": True},
            {
                "id": 2,
                "name": "alice__duplicate_2_1",
                "is_active": False,
            },
            {
                "id": 3,
                "name": "alice__duplicate_3",
                "is_active": False,
            },
            {
                "id": 4,
                "name": "alice__duplicate_2",
                "is_active": True,
            },
            {"id": 5, "name": "bob", "is_active": True},
        ]
        assert connection.execute(sa.select(passkeys.c.id, passkeys.c.user_id).order_by(passkeys.c.id)).all() == [
            (11, 2),
            (12, 3),
        ]
        index = _indexes(connection)["ux_user_name"]
        assert tuple(index["column_names"]) == ("name",)
        assert index["unique"] == 1
        assert "ix_user_name" not in _indexes(connection)

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    users.insert().values(
                        id=6,
                        name="alice",
                        is_active=True,
                    )
                )

        migration.downgrade()
        downgraded_rows = _user_rows(connection, users)
        assert downgraded_rows == upgraded_rows
        assert "ux_user_name" not in _indexes(connection)
        assert tuple(_indexes(connection)["ix_user_name"]["column_names"]) == ("name",)
        connection.execute(users.insert().values(id=6, name="alice", is_active=True))

        migration.upgrade()

        assert _user_rows(connection, users)[-1] == {
            "id": 6,
            "name": "alice__duplicate_6",
            "is_active": False,
        }
        assert connection.execute(sa.select(passkeys.c.user_id).order_by(passkeys.c.id)).scalars().all() == [2, 3]
        assert _indexes(connection)["ux_user_name"]["unique"] == 1


def test_migration_repairs_malformed_canonical_index(monkeypatch) -> None:
    """升级必须替换同名但列或唯一语义错误的部分迁移索引。"""
    engine = sa.create_engine("sqlite://")
    metadata, users, _passkeys = _legacy_metadata()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            users.insert(),
            [{"id": 1, "name": "alice", "is_active": True}],
        )
        connection.exec_driver_sql('CREATE INDEX ux_user_name ON "user" (is_active)')
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        index = _indexes(connection)["ux_user_name"]
        assert tuple(index["column_names"]) == ("name",)
        assert index["unique"] == 1


def test_migration_accepts_fresh_current_schema(monkeypatch) -> None:
    """当前模型已建表时，重复升级不得创建冲突索引。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        User.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        index = _indexes(connection)["ux_user_name"]
        assert tuple(index["column_names"]) == ("name",)
        assert index["unique"] == 1
