"""插件扩展媒体来源数据库迁移测试。"""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def _operations(connection: sa.Connection) -> Operations:
    """为内存 SQLite 连接构造 Alembic 操作对象。"""
    return Operations(MigrationContext.configure(connection))


def _create_fixed_constraint_table(connection: sa.Connection) -> None:
    """创建模拟已执行旧固定白名单 revision 的订阅表。"""
    metadata = sa.MetaData()
    sa.Table(
        "subscribe",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("media_source", sa.String),
        sa.Column("media_id", sa.String),
        sa.CheckConstraint(
            "(media_source IS NULL AND media_id IS NULL) OR "
            "(media_source IN ('themoviedb', 'douban') AND media_id IS NOT NULL "
            "AND trim(media_id) <> '' AND trim(media_id) <> '0')",
            name="ck_subscribe_media_identity",
        ),
    )
    metadata.create_all(connection)


def test_upgrade_replaces_fixed_source_whitelist(monkeypatch) -> None:
    """升级后应保留原数据、允许插件来源并继续拒绝非法身份。"""
    migration = importlib.import_module(
        "database.versions.b3d7e9f1a2c4_3_0_5"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        _create_fixed_constraint_table(connection)
        connection.execute(sa.text(
            "INSERT INTO subscribe (id, media_source, media_id) "
            "VALUES (1, 'themoviedb', '550')"
        ))
        monkeypatch.setattr(migration, "op", _operations(connection))

        migration.upgrade()

        connection.execute(sa.text(
            "INSERT INTO subscribe (id, media_source, media_id) "
            "VALUES (2, 'acme.video', 'custom-1')"
        ))
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(sa.text(
                    "INSERT INTO subscribe (id, media_source, media_id) "
                    "VALUES (3, 'invalid:source', 'custom-2')"
                ))
        rows = connection.execute(sa.text(
            "SELECT media_source, media_id FROM subscribe ORDER BY id"
        )).all()

    assert rows == [("themoviedb", "550"), ("acme.video", "custom-1")]
