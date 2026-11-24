"""
统一授权迁移对旧用户、管理员、默认接收人和重复绑定的离线验证。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.authorization import ChannelAuthorization
from app.db.models.user import User
from app.schemas.legacychannel import LEGACY_CHANNELS


def _tables(connection):
    """
    在独立 SQLite 连接中创建迁移所需旧表，不接触共享测试库。
    """
    metadata = sa.MetaData()
    User.__table__.to_metadata(metadata)
    system = sa.Table(
        "systemconfig",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.JSON()),
    )
    for name in ("agenttask", "agenttaskrun"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True), sa.Column("username", sa.String()))
    metadata.create_all(connection)
    return metadata.tables["user"], system


@pytest.mark.parametrize("channel_type,definition", LEGACY_CHANNELS.items())
def test_legacy_lists_migrate_per_instance_and_only_once(monkeypatch, channel_type, definition):
    """
    每个渠道完整迁移普通与管理员名单，主接收人保留但不会被重复导入覆盖新授权。
    """
    migration = importlib.import_module("database.versions.d8a4c6e29170_channel_authorization")
    engine = sa.create_engine("sqlite://")
    channel, users_key, admins_key, primary_key = definition
    values = {admins_key: " admin , overlap,admin ", "unrelated": "保留的配置"}
    if users_key:
        values[users_key] = "member,overlap"
    if primary_key:
        values[primary_key] = "primary"
    if channel_type == "wechat":
        values["WECHAT_MODE"] = "bot"
    with engine.begin() as connection:
        _, system = _tables(connection)
        connection.execute(
            system.insert().values(
                id=1,
                key="Notifications",
                value=[
                    {"id": "first", "name": "渠道A", "type": channel_type, "enabled": True, "config": values},
                    {
                        "id": "second",
                        "name": "渠道B",
                        "type": channel_type,
                        "enabled": False,
                        "config": {admins_key: "member"},
                    },
                ],
            )
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        members = sa.Table("channelauthorization", sa.MetaData(), autoload_with=connection)
        rows = list(connection.execute(sa.select(members)).mappings())
        first = {row["sender_id"]: row for row in rows if row["notification_id"] == "first"}
        assert first["admin"]["role"] == "admin" and first["overlap"]["role"] == "admin"
        assert all(row["channel"] == channel for row in rows)
        if users_key:
            assert first["member"]["role"] == "member"
        if primary_key:
            assert first["primary"]["role"] == "admin"
        assert next(row for row in rows if row["notification_id"] == "second")["role"] == "admin"
        persisted = connection.execute(sa.select(system.c.value)).scalar_one()
        assert admins_key not in persisted[0]["config"]
        assert not users_key or users_key not in persisted[0]["config"]
        assert persisted[0]["config"]["unrelated"] == "保留的配置"
        if primary_key:
            assert persisted[0]["config"][primary_key] == "primary"
        connection.execute(members.update().where(members.c.id == first["admin"]["id"]).values(status="disabled"))
        migration.upgrade()
        assert connection.execute(sa.select(sa.func.count()).select_from(members)).scalar_one() == len(rows)
        assert (
            connection.execute(sa.select(members.c.status).where(members.c.id == first["admin"]["id"])).scalar_one()
            == "disabled"
        )
        assert "execution_authorization_id" in {
            column["name"] for column in sa.inspect(connection).get_columns("agenttask")
        }


def test_migration_reuses_unique_account_and_keeps_ambiguous_identity_pending(monkeypatch):
    """
    唯一旧绑定保留业务归属，重复绑定不猜测账号，停用渠道也不丢失名单。
    """
    migration = importlib.import_module("database.versions.d8a4c6e29170_channel_authorization")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        users, system = _tables(connection)
        for user_id, sender in ((1, "unique"), (2, "duplicate"), (3, "duplicate")):
            connection.execute(
                users.insert().values(
                    id=user_id,
                    name=f"user{user_id}",
                    account_type="local",
                    notification_identity=f"identity-{user_id}",
                    settings={"telegram_userid": sender},
                )
            )
        connection.execute(
            system.insert().values(
                id=1,
                key="Notifications",
                value=[
                    {
                        "id": "telegram",
                        "name": "频道",
                        "type": "telegram",
                        "enabled": False,
                        "config": {"TELEGRAM_USERS": "unique,duplicate", "TELEGRAM_ADMINS": "admin"},
                    },
                ],
            )
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        members = sa.Table("channelauthorization", sa.MetaData(), autoload_with=connection)
        rows = {row["sender_id"]: row for row in connection.execute(sa.select(members)).mappings()}
        assert rows["unique"]["user_id"] == 1 and rows["unique"]["user_identity"] == "identity-1"
        assert rows["duplicate"]["status"] == "pending" and rows["duplicate"]["user_id"] not in {1, 2, 3}
        assert rows["admin"]["role"] == "admin"
        assert set(ChannelAuthorization.__table__.columns.keys()) == {
            column["name"] for column in sa.inspect(connection).get_columns("channelauthorization")
        }
