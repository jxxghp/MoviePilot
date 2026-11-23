"""
群授权隔离迁移的幂等、任务暂停及完整备份恢复回归。
"""

import importlib
import sqlite3

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _seed(connection):
    """
    创建可观察的旧授权与任务表并保留不同状态的合成记录。

    :param connection: 独立的临时数据库连接
    :return: 授权和任务表
    """
    metadata = sa.MetaData()
    members = sa.Table(
        "channelauthorization", metadata, sa.Column("id", sa.String, primary_key=True),
        sa.Column("channel", sa.String), sa.Column("sender_id", sa.String),
        sa.Column("status", sa.String), sa.Column("role", sa.String), sa.Column("scopes", sa.JSON),
        sa.Column("aliases", sa.JSON), sa.Column("origin", sa.String),
    )
    tasks = sa.Table(
        "agenttask", metadata, sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("execution_authorization_id", sa.String), sa.Column("channel", sa.String),
        sa.Column("user_id", sa.String), sa.Column("enabled", sa.Boolean),
    )
    metadata.create_all(connection)
    for identity, channel, sender, status in [
        ("qq-group", "QQ", "group:room", "approved"),
        ("voce-group", "VoceChat", "GID#2", "approved"),
        ("disabled", "QQ", "GROUP:disabled", "disabled"),
        ("rejected", "VoceChat", "gid#3", "rejected"),
        ("alice", "QQ", "alice", "approved"),
        ("voce-user", "VoceChat", "7", "approved"),
    ]:
        connection.execute(members.insert().values(
            id=identity, channel=channel, sender_id=sender, status=status, role="admin",
            scopes=["search", "schedule"], aliases=["group:alias"] if identity == "alice" else [], origin="legacy",
        ))
    for index, auth, channel, user in [
        (1, "qq-group", "QQ", "channel:qq-group"),
        (2, "voce-group", "VoceChat", "channel:voce-group"),
        (3, None, "QQ", "group:unbound"),
        (4, "alice", "QQ", "alice"),
    ]:
        connection.execute(tasks.insert().values(
            id=index, execution_authorization_id=auth, channel=channel, user_id=user, enabled=True,
        ))
    return members, tasks


def test_quarantine_is_idempotent_and_preserves_denials(monkeypatch, caplog):
    """
    旧群管理员失去执行能力，拒绝/停用不恢复，个人权限保留，日志仅记录计数。
    """
    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    with sa.create_engine("sqlite://").begin() as connection:
        members, tasks = _seed(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        with caplog.at_level("INFO", logger="alembic.runtime.migration"):
            migration._quarantine_group_authorizations()
        first = list(connection.execute(sa.select(members).order_by(members.c.id)).mappings())
        migration._quarantine_group_authorizations()
        assert list(connection.execute(sa.select(members).order_by(members.c.id)).mappings()) == first
        rows = {row["id"]: row for row in first}
        assert rows["qq-group"]["status"] == rows["voce-group"]["status"] == "pending"
        assert rows["disabled"]["status"] == "disabled" and rows["rejected"]["status"] == "rejected"
        assert all(rows[key]["role"] == "member" and rows[key]["scopes"] == []
                   for key in ("qq-group", "voce-group", "disabled", "rejected"))
        assert rows["alice"]["role"] == rows["voce-user"]["role"] == "admin"
        assert rows["alice"]["aliases"] == []
        assert dict(connection.execute(sa.select(tasks.c.id, tasks.c.enabled)).all()) == {
            1: False, 2: False, 3: False, 4: True,
        }
        assert "待处理群记录=4" in caplog.text and "group:room" not in caplog.text
        with pytest.raises(RuntimeError, match="备份"):
            migration.downgrade()


def test_complete_sqlite_backup_restores_preupgrade_state(tmp_path, monkeypatch):
    """
    完整 SQLite 备份可恢复升级前状态，测试不使用不安全的自动权限降级。
    """
    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    database = tmp_path / "before.db"
    backup = tmp_path / "snapshot.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        members, tasks = _seed(connection)
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration._quarantine_group_authorizations()
        assert connection.execute(sa.select(tasks.c.enabled).where(tasks.c.id == 1)).scalar_one() is False
    engine.dispose()
    with sqlite3.connect(backup) as source, sqlite3.connect(database) as destination:
        source.backup(destination)
    with engine.connect() as connection:
        assert connection.execute(sa.select(members.c.status).where(members.c.id == "qq-group")).scalar_one() == "approved"
        assert connection.execute(sa.select(tasks.c.enabled).where(tasks.c.id == 1)).scalar_one() is True
    engine.dispose()
