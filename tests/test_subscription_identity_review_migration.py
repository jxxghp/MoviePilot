"""
订阅身份唯一仲裁的无损升级、回放和双数据库语句合同。
"""

import importlib
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper, _insert_subscription


def test_identity_migration_preserves_duplicate_progress_and_replays(monkeypatch):
    """
    旧重复行、下载进度与主键原样保存，回放和降级不删除业务事实。
    """
    engine = sa.create_engine("sqlite://")
    migration = importlib.import_module("database.versions.c9e2f6a04173_subscription_identity")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE subscribe (id INTEGER PRIMARY KEY, username TEXT, type TEXT, "
            "media_source TEXT, media_id TEXT, season INTEGER, episode_group TEXT, music_type TEXT, note TEXT)"))
        for identifier, note in [(7, "[1]"), (8, "[2]")]:
            connection.execute(sa.text("INSERT INTO subscribe VALUES (:id, 'alice', '电视剧', "
                "'themoviedb', '123', 1, NULL, NULL, :note)"), {"id": identifier, "note": note})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        rows = connection.execute(sa.text("SELECT id,note,identity_key FROM subscribe ORDER BY id")).all()
        assert [(row[0], row[1]) for row in rows] == [(7, "[1]"), (8, "[2]")]
        assert rows[0][2] and rows[1][2] is None
        migration.upgrade()
        assert connection.execute(sa.text("SELECT id,note,identity_key FROM subscribe ORDER BY id")).all() == rows
        migration.downgrade()
        migration.downgrade()
        assert connection.execute(sa.text("SELECT id,note FROM subscribe ORDER BY id")).all() == [(7, "[1]"), (8, "[2]")]
    engine.dispose()


def test_legacy_duplicate_remains_existing_after_canonical_deletion(tmp_path):
    """
    canonical 删除后仍按真实身份找到旧重复行，不依赖仲裁键查询来判断已存在。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Subscribe.__table__.create(engine)
    values = {"name": "旧重复", "username": "alice", "type": "电视剧", "media_source": "themoviedb",
              "media_id": "123", "season": 1}
    with sa.orm.Session(engine) as session:
        canonical = Subscribe(**values)
        session.add(canonical)
        session.commit()
        canonical_id = canonical.id
    with engine.begin() as connection:
        connection.execute(Subscribe.__table__.insert().values(**values, identity_key=None))
        connection.execute(sa.delete(Subscribe).where(Subscribe.id == canonical_id))
    with sa.orm.Session(engine) as session:
        result = SubscribeOper(session).stage_add(values, values, "alice")
        session.commit()
        assert not result.created
        assert session.scalar(sa.select(sa.func.count()).select_from(Subscribe)) == 1
    engine.dispose()


def test_postgresql_identity_insert_uses_unique_conflict_arbiter():
    """
    PostgreSQL 使用原生 ON CONFLICT 身份仲裁，不使用进程锁代替跨进程唯一约束。
    """
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
    statement = str(_insert_subscription(session, {"name": "示例", "identity_key": "key"}).compile(
        dialect=postgresql.dialect(),
    ))
    assert "ON CONFLICT (identity_key) DO NOTHING" in statement
    index = next(index for index in Subscribe.__table__.indexes if index.name == "uq_subscribe_identity_key")
    assert "CREATE UNIQUE INDEX uq_subscribe_identity_key" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))


def test_identity_upgrade_chain_preserves_plugin_installation_snapshot(monkeypatch):
    """
    G06 最终前置与订阅迁移连续升降级，保留安装日志及旧订阅事实。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", "database")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["e4a8c7b29016"]
    assert script.get_revision("e4a8c7b29016").down_revision == "da73b6f20418"
    assert script.get_revision("da73b6f20418").down_revision == "c9e2f6a04173"
    assert script.get_revision("c9e2f6a04173").down_revision == "b8d2f5a01962"
    predecessor = importlib.import_module("database.versions.b8d2f5a01962_plugin_data_identity")
    migration = importlib.import_module("database.versions.c9e2f6a04173_subscription_identity")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE plugindata (id INTEGER PRIMARY KEY, plugin_id TEXT, "
                "key TEXT, value TEXT)"))
            connection.execute(sa.text("INSERT INTO plugindata VALUES (1,'review','key','keep'), "
                "(2,'review','key','old duplicate')"))
            connection.execute(sa.text("CREATE TABLE plugininstallation (id INTEGER PRIMARY KEY, phase TEXT)"))
            connection.execute(sa.text("INSERT INTO plugininstallation VALUES (7,'preparing')"))
            connection.execute(sa.text("CREATE TABLE subscribe (id INTEGER PRIMARY KEY, username TEXT, type TEXT, "
                "media_source TEXT, media_id TEXT, season INTEGER, episode_group TEXT, music_type TEXT, note TEXT)"))
            connection.execute(sa.text("INSERT INTO subscribe VALUES "
                "(7,'alice','电视剧','themoviedb','123',1,NULL,NULL,'[1]'), "
                "(8,'alice','电视剧','themoviedb','123',1,NULL,NULL,'[2]')"))
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(predecessor, "op", operations)
            monkeypatch.setattr(migration, "op", operations)
            for _ in range(2):
                predecessor.upgrade()
                migration.upgrade()
            assert connection.execute(sa.text("SELECT id,phase,host_enabled_before FROM plugininstallation")).all() == [
                (7, "preparing", None),
            ]
            assert connection.execute(sa.text("SELECT id,value FROM plugindata")).all() == [(1, "keep")]
            columns = {column["name"]: column for column in sa.inspect(connection).get_columns("plugininstallation")}
            assert columns["host_enabled_before"]["nullable"] is True
            before = connection.execute(sa.text("SELECT id,note,identity_key FROM subscribe ORDER BY id")).all()
            assert [(row[0], row[1]) for row in before] == [(7, "[1]"), (8, "[2]")]
            migration.downgrade()
            predecessor.downgrade()
            assert "host_enabled_before" not in {
                column["name"] for column in sa.inspect(connection).get_columns("plugininstallation")
            }
            assert connection.execute(sa.text("SELECT id,phase FROM plugininstallation")).all() == [(7, "preparing")]
            predecessor.upgrade()
            migration.upgrade()
            assert connection.execute(sa.text("SELECT id,note,identity_key FROM subscribe ORDER BY id")).all() == before
    finally:
        engine.dispose()
