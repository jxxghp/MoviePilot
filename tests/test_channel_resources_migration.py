"""
验证消息渠道最终迁移直接从正式版本升级，并保留已有最终版本数据库。
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.base import Base
from app.db.models import load_all_models
from app.schemas.channel import ChannelSettings


def _config(path: Path) -> Config:
    """
    为独占的临时 SQLite 构造真实 Alembic 配置。

    :param path: 当前用例独占的数据库路径
    :return: 只连接临时库的迁移配置
    """
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "database"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _formal_database(engine) -> None:
    """
    建立正式版本涉及的旧表，并安排可观察的权限、业务和通知记录。

    :param engine: 当前用例独占的数据库引擎
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT)")
        connection.exec_driver_sql("INSERT INTO user VALUES (1, 'alice')")
        connection.exec_driver_sql("CREATE TABLE channelauthorization (id TEXT PRIMARY KEY, channel TEXT, "
                                   "sender_id TEXT, aliases JSON, status TEXT, role TEXT, scopes JSON, origin TEXT)")
        connection.exec_driver_sql("INSERT INTO channelauthorization VALUES "
                                   "('group', 'QQ', 'group:room', '[]', 'approved', 'admin', '[\"schedule\"]', 'legacy')")
        connection.exec_driver_sql("CREATE TABLE agenttask (id INTEGER PRIMARY KEY, execution_authorization_id TEXT, "
                                   "channel TEXT, user_id TEXT, enabled BOOLEAN)")
        connection.exec_driver_sql("INSERT INTO agenttask VALUES (1, 'group', 'QQ', 'group:room', 1)")
        for table in ("subscribe", "subscribehistory"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, username TEXT)")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1, 'alice')")
        connection.exec_driver_sql("CREATE TABLE downloadhistory (id INTEGER PRIMARY KEY, username TEXT, date TEXT)")
        connection.exec_driver_sql("INSERT INTO downloadhistory VALUES (1, 'alice', '2026-01-01')")
        connection.exec_driver_sql("CREATE TABLE outboxmessage (id INTEGER PRIMARY KEY, event_key TEXT)")
        connection.exec_driver_sql("INSERT INTO outboxmessage VALUES (1, 'preserve-event')")
        table = sa.Table("systemconfig", sa.MetaData(), sa.Column("key", sa.String, primary_key=True),
                         sa.Column("value", sa.JSON))
        table.create(connection)
        connection.execute(table.insert().values(key="Notifications", value=[{
            "id": "telegram", "name": "Telegram", "type": "telegram", "enabled": True,
            "switchs": ["订阅"], "config": {"TELEGRAM_CHAT_ID": "-100123"},
        }]))


def test_formal_database_upgrades_directly_to_final_resources(tmp_path):
    """
    正式版本到最终 head 只执行一个迁移，保留业务数据、隔离群授权并生成可直接读取的规则。
    """
    database = tmp_path / "formal.sqlite"
    engine = sa.create_engine(f"sqlite:///{database}")
    _formal_database(engine)
    config = _config(database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["62c91ad8b057"]
    assert [revision.revision for revision in script.iterate_revisions("head", "c4e8f1a29b03")] == ["62c91ad8b057"]
    command.stamp(config, "d8a4c6e29170")
    command.upgrade(config, "c4e8f1a29b03")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT status, role, scopes FROM channelauthorization").one() == (
            "pending", "member", "[]",
        )
        assert connection.exec_driver_sql("SELECT enabled FROM agenttask").scalar_one() == 0
        for table in ("subscribe", "subscribehistory", "downloadhistory"):
            assert connection.exec_driver_sql(f"SELECT username, owner_identity FROM {table}").one() == ("alice", None)
        assert connection.exec_driver_sql("SELECT event_key, notification_progress FROM outboxmessage").one() == (
            "preserve-event", None,
        )
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM webpushsubscription").scalar_one() == 0
        table = sa.Table("systemconfig", sa.MetaData(), autoload_with=connection)
        raw = connection.execute(sa.select(table.c.value).where(table.c.key == "ChannelSettings")).scalar_one()
        settings = ChannelSettings.model_validate(raw)
        assert settings.notifications["telegram"].rules[1].target == "-100123"
        assert settings.interactions["telegram"].admission == "manual"
        assert settings.interactions["telegram"].auto_download == "off"
        assert "TELEGRAM_CHAT_ID" not in settings.connections["telegram"].provider_config
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "62c91ad8b057"
        assert {"channelinboundreceipt", "channelruletaskgrant", "webpushsubscription"} <= set(
            sa.inspect(connection).get_table_names()
        )
        assert "ix_downloadhistory_task_reference" in {
            index["name"] for index in sa.inspect(connection).get_indexes("downloadhistory")
        }
    with pytest.raises(RuntimeError, match="备份"):
        command.downgrade(config, "c4e8f1a29b03")
    engine.dispose()


@pytest.mark.parametrize("current_revision", ["c4e8f1a29b03", "62c91ad8b057"])
def test_final_metadata_and_existing_final_revision_remain_usable(tmp_path, current_revision):
    """
    首次建表后的迁移和已处于最终 head 的数据库均不重复建表，也不需要改写版本标记。
    """
    database = tmp_path / "current.sqlite"
    engine = sa.create_engine(f"sqlite:///{database}")
    load_all_models()
    Base.metadata.create_all(engine)
    config = _config(database)
    command.stamp(config, current_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "62c91ad8b057"
        assert {"owner_identity", "task_reference"} <= {
            column["name"] for column in sa.inspect(connection).get_columns("downloadhistory")
        }
        assert "notification_progress" in {column["name"] for column in sa.inspect(connection).get_columns("outboxmessage")}
    engine.dispose()
