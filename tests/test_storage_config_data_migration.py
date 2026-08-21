"""存量存储配置从 systemconfig 搬进服务实例配置表。

存量配置是一个存储类型一份，搬迁后成为该存储类型的具名实例，并标记为该类型的裸令牌
兼容指针——裸令牌 ``u115`` 落到它，不标记则所有存量路径 ``u115:/media`` 会整体失效。

兼容指针落在实例级宿主载荷而不是「默认调用目标」列：后者每族至多一行、回答的是「调用
没指定存储时用哪个」，而兼容指针是每个存储类型各一个、回答的是「地址缺实例段时落到哪份」。
搬迁写的仍是旧键名，随后由改名那一步统一成 ``bare_token_target``。
"""
import importlib
import json

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION = "database.versions.f8767f021120_3_0_8"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _prepare_systemconfig(connection, values: dict) -> None:
    """建出 systemconfig 表并写入给定的键值。

    :param connection: 迁移所用的数据库连接
    :param values: 配置键到配置值的映射
    :return: 无返回值
    """
    connection.exec_driver_sql(
        "CREATE TABLE systemconfig (id INTEGER PRIMARY KEY, key VARCHAR, value JSON)"
    )
    for key, value in values.items():
        connection.exec_driver_sql(
            "INSERT INTO systemconfig (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def _storage_rows(connection) -> list:
    """回读服务实例配置表里的存储族配置，按主键升序即写入先后。

    :param connection: 迁移所用的数据库连接
    :return: 每行的列字典
    """
    rows = connection.exec_driver_sql(
        "SELECT type, name, enabled, config, host_config, is_default_target, provider "
        "FROM serviceconfig WHERE capability = 'storage' ORDER BY id"
    ).fetchall()
    return [
        {
            "type": row[0],
            "name": row[1],
            "enabled": bool(row[2]),
            "config": json.loads(row[3]) if row[3] else None,
            "host_config": json.loads(row[4]) if row[4] else None,
            "is_default_target": bool(row[5]),
            "provider": row[6],
        }
        for row in rows
    ]


def test_migration_turns_each_stored_type_into_its_bare_token_target(monkeypatch) -> None:
    """存量的一个类型一份配置搬成具名实例，并成为该类型的裸令牌兼容指针。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [
                {"type": "local", "name": "本地"},
                {"type": "u115", "name": "115网盘", "config": {"refresh_token": "t"}},
                {"type": "alipan", "name": "阿里云盘", "config": {"drive_id": "1"}},
            ],
        })

        migration.upgrade()

        rows = {row["type"]: row for row in _storage_rows(connection)}
        assert set(rows) == {"local", "u115", "alipan"}
        assert rows["u115"]["name"] == "115网盘"
        assert rows["u115"]["config"] == {"refresh_token": "t"}
        assert rows["u115"]["host_config"] == {"bare_token_target": True}
        assert rows["u115"]["enabled"] is True
        assert {row["host_config"]["bare_token_target"] for row in rows.values()} == {True}
        assert {row["is_default_target"] for row in rows.values()} == {False}
        assert {row["provider"] for row in rows.values()} == {"host:builtin"}


def test_migration_names_a_nameless_entry_after_its_storage_type(monkeypatch) -> None:
    """存量名称是可空的展示名，取不到时以存储类型为实例名。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [{"type": "u115", "config": {"refresh_token": "t"}}],
        })

        migration.upgrade()

        rows = _storage_rows(connection)
        assert [row["name"] for row in rows] == ["u115"]
        assert rows[0]["host_config"] == {"bare_token_target": True}


def test_migration_drops_entries_without_a_storage_type(monkeypatch) -> None:
    """取不到存储类型的条目丢弃，表按 (族, 类型, 实例名) 定位一行，装不下它们。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [
                {"name": "没有类型"},
                {"type": "   ", "name": "空白类型"},
                "不是对象",
                {"type": "u115", "name": "有类型"},
            ],
        })

        migration.upgrade()

        assert [row["name"] for row in _storage_rows(connection)] == ["有类型"]


def test_migration_gives_one_bare_token_target_per_storage_type(monkeypatch) -> None:
    """兼容指针的作用域是存储类型，每个类型各有一个，取顺序上第一份。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [
                {"type": "u115", "name": "主号"},
                {"type": "u115", "name": "副号"},
                {"type": "alipan", "name": "阿里"},
            ],
        })

        migration.upgrade()

        pointers = [
            (row["type"], row["name"]) for row in _storage_rows(connection)
            if row["host_config"]["bare_token_target"]
        ]
        assert pointers == [("u115", "主号"), ("alipan", "阿里")]


def test_migration_lets_the_later_duplicate_win(monkeypatch) -> None:
    """同类型同名的存量条目后者覆盖前者。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [
                {"type": "u115", "name": "115网盘", "config": {"token": "旧"}},
                {"type": "u115", "name": "115网盘", "config": {"token": "新"}},
            ],
        })

        migration.upgrade()

        rows = _storage_rows(connection)
        assert len(rows) == 1
        assert rows[0]["config"] == {"token": "新"}


def test_migration_is_idempotent_across_repeated_upgrades(monkeypatch) -> None:
    """重跑迁移结果不变：存储族已有行即跳过，否则重复搬迁会叠出重复实例。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [{"type": "u115", "name": "115网盘", "config": {"token": "t"}}],
        })

        migration.upgrade()
        first = _storage_rows(connection)
        migration.upgrade()
        migration.upgrade()

        assert _storage_rows(connection) == first


def test_migration_skips_when_the_storage_family_already_has_rows(monkeypatch) -> None:
    """存储族里已有用户数据时跳过，不把 systemconfig 的快照再叠一遍。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [{"type": "u115", "name": "115网盘"}],
        })
        migration.upgrade()
        connection.exec_driver_sql("DELETE FROM serviceconfig WHERE capability = 'storage'")
        connection.exec_driver_sql(
            "INSERT INTO serviceconfig "
            "(capability, type, name, enabled, is_default_target, provider) "
            "VALUES ('storage', 'u115', '用户自建', 1, 0, 'host:builtin')"
        )

        migration.upgrade()

        assert [row["name"] for row in _storage_rows(connection)] == ["用户自建"]


def test_rename_step_converts_rows_left_by_an_earlier_upgrade(monkeypatch) -> None:
    """已经搬过的库里旧键 is_default 被改名，取值原样搬到 bare_token_target。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {"Storages": [{"type": "u115", "name": "旧"}]})
        migration.upgrade()
        connection.exec_driver_sql("DELETE FROM serviceconfig WHERE capability = 'storage'")
        for name, mark in (("主号", True), ("副号", False)):
            connection.exec_driver_sql(
                "INSERT INTO serviceconfig "
                "(capability, type, name, enabled, host_config, is_default_target, provider) "
                "VALUES ('storage', 'u115', ?, 1, ?, 0, 'host:builtin')",
                (name, json.dumps({"is_default": mark})),
            )

        migration.upgrade()

        rows = {row["name"]: row["host_config"] for row in _storage_rows(connection)}
        assert rows == {
            "主号": {"bare_token_target": True}, "副号": {"bare_token_target": False}
        }


def test_rename_step_leaves_already_renamed_rows_alone(monkeypatch) -> None:
    """改名那一步随每次升级重放，已经是新键名的行原样跳过。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Storages": [
                {"type": "u115", "name": "主号"},
                {"type": "u115", "name": "副号"},
            ],
        })

        migration.upgrade()
        first = _storage_rows(connection)
        migration.upgrade()

        assert _storage_rows(connection) == first
        assert [row["host_config"] for row in first] == [
            {"bare_token_target": True}, {"bare_token_target": False}
        ]


def test_migration_is_independent_from_the_three_service_families(monkeypatch) -> None:
    """三族服务实例配置与存储各搬各的，一族已有行不该把另一族挡在门外。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [{"name": "qb", "type": "qbittorrent", "enabled": True}],
            "Storages": [{"type": "u115", "name": "115网盘"}],
        })

        migration.upgrade()

        assert [row["name"] for row in _storage_rows(connection)] == ["115网盘"]
        downloaders = connection.exec_driver_sql(
            "SELECT name FROM serviceconfig WHERE capability = 'downloader'"
        ).fetchall()
        assert [row[0] for row in downloaders] == ["qb"]


def test_migration_does_not_touch_the_systemconfig_key(monkeypatch) -> None:
    """systemconfig 上的存储配置键只停写不删，回退时读取端改回去即可复原。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        stored = [{"type": "u115", "name": "115网盘", "config": {"token": "t"}}]
        _prepare_systemconfig(connection, {"Storages": stored})

        migration.upgrade()
        migration.downgrade()

        value = connection.exec_driver_sql(
            "SELECT value FROM systemconfig WHERE key = 'Storages'"
        ).scalar()
        assert json.loads(value) == stored


def test_migration_tolerates_a_database_without_storage_config(monkeypatch) -> None:
    """全新安装没有存量存储配置可搬，建完表即结束，不因缺键失败。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {})

        migration.upgrade()

        assert _storage_rows(connection) == []
