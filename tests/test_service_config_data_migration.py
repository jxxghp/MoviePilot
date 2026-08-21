"""存量服务实例配置从 systemconfig 搬进服务实例配置表。

搬迁只做一件事：把 ``Downloaders`` / ``MediaServers`` / ``Notifications`` 三个键里的
JSON 列表整形成表里的行。systemconfig 上那三个键只停写不删，因此搬迁不动它们——回退
就是把读取端改回去。

存量数据有四类脏法必须逐一处置：同身份重名、取不到身份、多条或零条默认标记、以及
提供方不可考。四条的口径一律是「与切表前的运行期行为一致」，这样搬完之后用户看到的
实例与搬之前逐条相同。
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


def _migrated_rows(connection) -> list:
    """回读服务实例配置表，按主键升序即写入先后。

    :param connection: 迁移所用的数据库连接
    :return: 每行的列字典
    """
    rows = connection.exec_driver_sql(
        "SELECT capability, type, name, enabled, config, host_config, "
        "is_default_target, provider FROM serviceconfig ORDER BY id"
    ).fetchall()
    return [
        {
            "capability": row[0],
            "type": row[1],
            "name": row[2],
            "enabled": bool(row[3]),
            "config": json.loads(row[4]) if row[4] else None,
            "host_config": json.loads(row[5]) if row[5] else None,
            "is_default_target": bool(row[6]),
            "provider": row[7],
        }
        for row in rows
    ]


def test_migration_moves_three_families_and_splits_fields_by_consumer(monkeypatch) -> None:
    """
    存量三族配置搬进表，字段按消费方分列。

    类型实现自己读的内容留在 ``config``，宿主自己读的实例级字段进 ``host_config``；
    两者混放会让声明了 ``additionalProperties: false`` 的类型把宿主字段判为违约。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [{
                "name": "qb", "type": "qbittorrent", "enabled": True, "default": True,
                "config": {"host": "h"}, "path_mapping": [["/media", "/dl"]],
            }],
            "MediaServers": [{
                "name": "emby", "type": "emby", "enabled": True,
                "config": {"host": "e"}, "sync_libraries": ["1"], "sync_interval": 6,
            }],
            "Notifications": [{
                "name": "tg", "type": "telegram", "enabled": False,
                "config": {"token": "t"}, "switchs": ["Manual"],
            }],
        })

        migration.upgrade()

        rows = {row["name"]: row for row in _migrated_rows(connection)}
        assert set(rows) == {"qb", "emby", "tg"}
        assert rows["qb"]["capability"] == "downloader"
        assert rows["qb"]["config"] == {"host": "h"}
        assert rows["qb"]["host_config"] == {"path_mapping": [["/media", "/dl"]]}
        assert rows["qb"]["is_default_target"] is True
        assert rows["emby"]["host_config"] == {"sync_libraries": ["1"], "sync_interval": 6}
        assert rows["tg"]["host_config"] == {"switchs": ["Manual"]}
        assert rows["tg"]["enabled"] is False
        assert {row["provider"] for row in rows.values()} == {"host:builtin"}


def test_migration_is_idempotent_across_repeated_upgrades(monkeypatch) -> None:
    """重跑迁移结果不变：表非空即整体跳过，否则回退再升级会产生重复行。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [
                {"name": "qb", "type": "qbittorrent", "enabled": True},
                {"name": "tr", "type": "transmission", "enabled": True, "default": True},
            ],
        })

        migration.upgrade()
        first = _migrated_rows(connection)
        migration.upgrade()
        migration.upgrade()

        assert _migrated_rows(connection) == first
        assert [row["name"] for row in first] == ["qb", "tr"]


def test_migration_skips_when_the_table_already_has_rows(monkeypatch) -> None:
    """表里已有用户数据时整体跳过，不把 systemconfig 的快照再叠一遍。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [{"name": "qb", "type": "qbittorrent", "enabled": True}],
        })
        migration.upgrade()
        connection.exec_driver_sql("DELETE FROM serviceconfig WHERE name = 'qb'")
        connection.exec_driver_sql(
            "INSERT INTO serviceconfig "
            "(capability, type, name, enabled, is_default_target, provider) "
            "VALUES ('downloader', 'qbittorrent', 'user-added', 1, 0, 'host:builtin')"
        )

        migration.upgrade()

        assert [row["name"] for row in _migrated_rows(connection)] == ["user-added"]


def test_migration_drops_entries_without_an_identity(monkeypatch) -> None:
    """
    取不到名称或类型的条目丢弃。

    这类条目在切表前就不产出任何实例（扇出只认具名且类型一致的配置），表也按身份三元组
    定位一行，装不下它们。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [
                {"type": "qbittorrent", "enabled": True},
                {"name": "   ", "type": "qbittorrent", "enabled": True},
                {"name": "无类型", "enabled": True},
                "不是对象",
                {"name": "有身份", "type": "qbittorrent", "enabled": True},
            ],
        })

        migration.upgrade()

        assert [row["name"] for row in _migrated_rows(connection)] == ["有身份"]


def test_migration_lets_the_later_duplicate_win(monkeypatch) -> None:
    """同身份的存量条目后者覆盖前者，与切表前扇出按名字建映射的结果一致。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [
                {"name": "qb", "type": "qbittorrent", "enabled": False,
                 "config": {"host": "旧"}},
                {"name": "qb", "type": "qbittorrent", "enabled": True,
                 "config": {"host": "新"}},
            ],
        })

        migration.upgrade()

        rows = _migrated_rows(connection)
        assert len(rows) == 1
        assert rows[0]["config"] == {"host": "新"}
        assert rows[0]["enabled"] is True


def test_migration_arbitrates_multiple_default_marks(monkeypatch) -> None:
    """多条 default 为真时只留第一条，否则直接撞上「每族至多一个默认调用目标」。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [
                {"name": "甲", "type": "qbittorrent", "enabled": True, "default": True},
                {"name": "乙", "type": "transmission", "enabled": True, "default": True},
                {"name": "丙", "type": "rtorrent", "enabled": True, "default": True},
            ],
        })

        migration.upgrade()

        rows = _migrated_rows(connection)
        assert [row["name"] for row in rows if row["is_default_target"]] == ["甲"]


def test_migration_leaves_the_family_without_a_default_when_none_is_marked(monkeypatch) -> None:
    """一条 default 都没有时该族就是没有默认调用目标，不替用户指定一个。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [
                {"name": "甲", "type": "qbittorrent", "enabled": True},
                {"name": "乙", "type": "transmission", "enabled": True},
            ],
        })

        migration.upgrade()

        rows = _migrated_rows(connection)
        assert [row["name"] for row in rows if row["is_default_target"]] == []


def test_migration_keeps_each_family_default_independent(monkeypatch) -> None:
    """默认调用目标的作用域是族，三族可以各有一个。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        _prepare_systemconfig(connection, {
            "Downloaders": [{"name": "qb", "type": "qbittorrent", "default": True}],
            "MediaServers": [{"name": "emby", "type": "emby", "default": True}],
            "Notifications": [{"name": "tg", "type": "telegram", "default": True}],
        })

        migration.upgrade()

        defaults = {
            row["capability"] for row in _migrated_rows(connection)
            if row["is_default_target"]
        }
        assert defaults == {"downloader", "mediaserver", "notification"}


def test_migration_does_not_touch_the_systemconfig_keys(monkeypatch) -> None:
    """systemconfig 的三个键只停写不删，回退时读取端改回去即可复原。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        stored = [{"name": "qb", "type": "qbittorrent", "enabled": True}]
        _prepare_systemconfig(connection, {"Downloaders": stored})

        migration.upgrade()
        migration.downgrade()

        value = connection.exec_driver_sql(
            "SELECT value FROM systemconfig WHERE key = 'Downloaders'"
        ).scalar()
        assert json.loads(value) == stored


def test_migration_tolerates_a_database_without_systemconfig(monkeypatch) -> None:
    """全新安装没有存量配置可搬，建完表即结束，不因缺表失败。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert _migrated_rows(connection) == []
