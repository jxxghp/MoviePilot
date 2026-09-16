import copy
import importlib
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError
from fastapi import FastAPI
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine.url import make_url

from app.application.classification.configuration import (
    build_default_classification_policy,
)
from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.health import get_application_health
from app.schemas.category import ClassificationCategory, ClassificationPolicyState
from app.startup import lifecycle
from app.startup.composition import database as startup_database
from app.startup.initializers import database as db_init

LOCAL_SETUP_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"
)
_CLASSIFICATION_CONFIG_KEYS = (
    "MediaClassificationPolicy",
    "Directories",
)


@pytest.fixture
def isolated_classification_config(db):
    """隔离离线配置用例使用的策略和目录键，并恢复共享测试库快照。"""
    db.watermark(SystemConfig)
    snapshots = [
        (row.id, row.key, copy.deepcopy(row.value))
        for row in db.session.execute(
            sa.select(SystemConfig).where(
                SystemConfig.key.in_(_CLASSIFICATION_CONFIG_KEYS)
            )
        ).scalars()
    ]
    db.session.execute(
        sa.delete(SystemConfig).where(
            SystemConfig.key.in_(_CLASSIFICATION_CONFIG_KEYS)
        )
    )
    db.session.commit()
    SystemConfigOper().load_snapshot(db.session)
    try:
        yield
    finally:
        db.session.rollback()
        db.session.execute(
            sa.delete(SystemConfig).where(
                SystemConfig.key.in_(_CLASSIFICATION_CONFIG_KEYS)
            )
        )
        for row_id, key, value in snapshots:
            db.session.add(SystemConfig(id=row_id, key=key, value=value))
        db.session.commit()
        SystemConfigOper().load_snapshot(db.session)


def _load_local_setup_module():
    """加载隔离的本地安装脚本实例，避免测试间共享模块状态。"""
    module_name = f"moviepilot_local_setup_migration_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, LOCAL_SETUP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_db_preserves_migration_error_and_traceback(monkeypatch) -> None:
    """迁移失败日志应保留堆栈，同时向调用方传播原始异常。"""
    migration_error = RuntimeError("migration failed")
    logged_errors: list[str] = []

    def fail_upgrade(*_args, **_kwargs) -> None:
        raise migration_error

    monkeypatch.setattr(db_init, "upgrade", fail_upgrade)
    monkeypatch.setattr(db_init.logger, "error", logged_errors.append)

    with pytest.raises(RuntimeError) as raised:
        db_init.update_db()

    assert raised.value is migration_error
    assert len(logged_errors) == 1
    assert "数据库更新失败：migration failed" in logged_errors[0]
    assert "RuntimeError: migration failed" in logged_errors[0]


def test_prepare_database_creates_backup_before_schema_changes(monkeypatch) -> None:
    """既有数据库待迁移时，恢复点必须早于所有结构写入。"""
    calls: list[str] = []
    logged_messages: list[str] = []
    governance = Mock()
    governance.create_backup.side_effect = lambda: calls.append("backup")
    monkeypatch.setattr(db_init, "get_engine", lambda: object())
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _engine: object())
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (True, ("old",), ("head",)),
    )
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ON_UPGRADE", True)
    monkeypatch.setattr(
        db_init,
        "build_database_governance",
        lambda: governance,
    )
    monkeypatch.setattr(db_init.logger, "info", logged_messages.append)
    monkeypatch.setattr(db_init, "init_db", lambda: calls.append("create_all"))
    monkeypatch.setattr(
        db_init,
        "update_db",
        lambda _config: calls.append("alembic"),
    )

    db_init.prepare_database(before_alembic=lambda: calls.append("before_alembic"))

    assert calls == [
        "backup",
        "alembic",
        "create_all",
        "before_alembic",
    ]
    assert logged_messages == [
        "数据库需要从版本 old 升级到 head，正在创建迁移前备份"
    ]


def test_prepare_database_keeps_create_all_first_for_unversioned_database(
        monkeypatch,
) -> None:
    """未标记旧库仍需先补齐基础表，再从 Alembic base 执行数据迁移。"""
    calls: list[str] = []
    monkeypatch.setattr(db_init, "get_engine", lambda: object())
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _engine: object())
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (True, (), ("head",)),
    )
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ENABLE", False)
    monkeypatch.setattr(db_init, "init_db", lambda: calls.append("create_all"))
    monkeypatch.setattr(
        db_init,
        "update_db",
        lambda _config: calls.append("alembic"),
    )

    db_init.prepare_database(before_alembic=lambda: calls.append("before_alembic"))

    assert calls == ["create_all", "before_alembic", "alembic"]


@pytest.mark.parametrize(
    ("has_existing_database", "current_heads", "backup_enabled", "upgrade_enabled"),
    (
        (False, (), True, True),
        (True, ("head",), True, True),
        (True, ("old",), False, True),
        (True, ("old",), True, False),
    ),
)
def test_prepare_database_skips_backup_outside_enabled_pending_migration(
        monkeypatch,
        has_existing_database: bool,
        current_heads: tuple[str, ...],
        backup_enabled: bool,
        upgrade_enabled: bool,
) -> None:
    """全新库、已到 head 或关闭保护时不创建自动恢复点。"""
    governance = Mock()
    monkeypatch.setattr(db_init, "get_engine", lambda: object())
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _engine: object())
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (has_existing_database, current_heads, ("head",)),
    )
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ENABLE", backup_enabled)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ON_UPGRADE", upgrade_enabled)
    monkeypatch.setattr(
        db_init,
        "build_database_governance",
        lambda: governance,
    )
    monkeypatch.setattr(db_init, "init_db", lambda: None)
    monkeypatch.setattr(db_init, "update_db", lambda _config: None)

    db_init.prepare_database()

    governance.create_backup.assert_not_called()


def test_prepare_database_stops_before_schema_changes_when_backup_fails(
        monkeypatch,
) -> None:
    """迁移保护失败时不得继续执行 create_all 或 Alembic。"""
    backup_error = RuntimeError("backup failed")
    init_calls: list[None] = []
    governance = Mock()
    governance.create_backup.side_effect = backup_error
    monkeypatch.setattr(db_init, "get_engine", lambda: object())
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _engine: object())
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (True, ("old",), ("head",)),
    )
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ON_UPGRADE", True)
    monkeypatch.setattr(
        db_init,
        "build_database_governance",
        lambda: governance,
    )
    monkeypatch.setattr(db_init, "init_db", lambda: init_calls.append(None))
    monkeypatch.setattr(db_init, "update_db", lambda _config: init_calls.append(None))

    with pytest.raises(RuntimeError) as raised:
        db_init.prepare_database(
            before_alembic=lambda: init_calls.append(None),
        )

    assert raised.value is backup_error
    assert init_calls == []


def test_alembic_config_uses_active_engine_url_without_hiding_password() -> None:
    """Alembic 必须连接活动引擎目标，内部配置不得把密码替换为星号。"""
    engine = SimpleNamespace(
        url=make_url("postgresql://moviepilot:secret@database/moviepilot")
    )

    config = db_init._build_alembic_config(engine)

    assert config.get_main_option("sqlalchemy.url") == (
        "postgresql://moviepilot:secret@database/moviepilot"
    )


def test_migration_state_distinguishes_fresh_legacy_and_current_sqlite(
        tmp_path: Path,
) -> None:
    """SQLite 空库不备份，已有业务表且无 revision 时识别为待迁移。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    config = db_init._build_alembic_config(engine)
    target_heads = tuple(db_init.ScriptDirectory.from_config(config).get_heads())

    assert db_init._migration_state(engine, config) == (False, (), target_heads)

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)"))

    assert db_init._migration_state(engine, config) == (True, (), target_heads)

    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": target_heads[0]},
        )

    assert db_init._migration_state(engine, config) == (
        True,
        target_heads,
        target_heads,
    )


def test_migration_state_rejects_unknown_revision_before_schema_writes(
        tmp_path: Path,
) -> None:
    """未知或更高版本 revision 不得被误判为可执行升级。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'future.db'}")
    config = db_init._build_alembic_config(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('future')")
        )

    with pytest.raises(RuntimeError, match="无法识别数据库 revision：future"):
        db_init._migration_state(engine, config)

    assert set(db_init.inspect(engine).get_table_names()) == {
        "alembic_version",
        "legacy_data",
    }


def test_migration_lineage_rejects_multiple_heads() -> None:
    """当前迁移执行器仅接受仓库和数据库均保持单一 head。"""
    script = Mock()

    with pytest.raises(RuntimeError, match="迁移脚本必须只有一个 head"):
        db_init._validate_migration_lineage(script, (), ("head-a", "head-b"))

    with pytest.raises(RuntimeError, match="多个 current revision"):
        db_init._validate_migration_lineage(
            script,
            ("current-a", "current-b"),
            ("head",),
        )


def test_migration_lineage_rejects_known_divergent_revision() -> None:
    """可识别但不在目标祖先链上的 revision 不得继续自动迁移。"""
    script = Mock()
    script.walk_revisions.return_value = (
        SimpleNamespace(revision="head"),
        SimpleNamespace(revision="base"),
    )

    with pytest.raises(RuntimeError, match="不是当前 head head 的可升级祖先"):
        db_init._validate_migration_lineage(
            script,
            ("other-branch",),
            ("head",),
        )
    script.get_revision.assert_called_once_with("other-branch")


def test_migration_lineage_wraps_unknown_revision() -> None:
    """Alembic 未知 revision 错误应转换为可操作的启动错误。"""
    script = Mock()
    script.get_revision.side_effect = CommandError("unknown")

    with pytest.raises(RuntimeError, match="无法识别数据库 revision：future"):
        db_init._validate_migration_lineage(
            script,
            ("future",),
            ("head",),
        )


def test_verify_database_revision_requires_current_head(monkeypatch) -> None:
    """readiness 的数据库校验必须拒绝升级后仍未到 head 的状态。"""
    engine = object()
    config = object()
    monkeypatch.setattr(db_init, "get_engine", lambda: engine)
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _: config)
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (True, ("old",), ("head",)),
    )

    with pytest.raises(RuntimeError, match="仍未到达当前 head"):
        db_init.verify_database_revision()


def test_verify_database_revision_accepts_current_head(monkeypatch) -> None:
    """活动 revision 与唯一目标 head 一致时允许发布数据库就绪。"""
    engine = object()
    config = object()
    monkeypatch.setattr(db_init, "get_engine", lambda: engine)
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _: config)
    monkeypatch.setattr(
        db_init,
        "_migration_state",
        lambda *_: (True, ("head",), ("head",)),
    )

    db_init.verify_database_revision()


def test_lifecycle_database_component_marks_ready_after_head_check(
    monkeypatch,
) -> None:
    """数据库组件必须按迁移、head 校验、发布状态的顺序执行。"""
    app = FastAPI()
    calls: list[str] = []
    monkeypatch.setattr(
        db_init,
        "prepare_database",
        lambda: calls.append("prepare"),
    )
    monkeypatch.setattr(
        db_init,
        "verify_database_revision",
        lambda: calls.append("verify"),
    )

    lifecycle.prepare_database_component(app)

    assert calls == ["prepare", "verify"]
    assert get_application_health(app).database_ready is True


def test_prepare_database_creates_real_sqlite_restore_point_before_upgrade(
        tmp_path: Path,
        monkeypatch,
) -> None:
    """迁移前备份保留旧版本，活动 SQLite 升级到目标版本。"""
    script_root = tmp_path / "database"
    versions = script_root / "versions"
    versions.mkdir(parents=True)
    (script_root / "env.py").write_text(
        """
from alembic import context
from sqlalchemy import engine_from_config, pool

engine = engine_from_config(
    context.config.get_section(context.config.config_ini_section),
    prefix="sqlalchemy.",
    poolclass=pool.NullPool,
)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
""".strip(),
        encoding="utf-8",
    )
    (versions / "001_base.py").write_text(
        """
revision = "001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    pass
""".strip(),
        encoding="utf-8",
    )
    (versions / "002_head.py").write_text(
        """
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("records", sa.Column("migrated", sa.Integer()))

def downgrade():
    op.drop_column("records", "migrated")
""".strip(),
        encoding="utf-8",
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'active.db'}")
    metadata = MetaData()
    Table("records", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('001')")
        )

    config = db_init._build_alembic_config(engine)
    config.set_main_option("script_location", str(script_root))
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_ON_UPGRADE", True)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_PATH", str(tmp_path / "backups"))
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_RETENTION_DAYS", 0)
    monkeypatch.setattr(db_init.settings, "DB_BACKUP_MAX_COUNT", 0)
    monkeypatch.setattr(db_init, "get_engine", lambda: engine)
    monkeypatch.setattr(db_init, "_build_alembic_config", lambda _engine: config)
    monkeypatch.setattr(db_init, "Base", SimpleNamespace(metadata=metadata))
    monkeypatch.setattr(db_init, "load_all_models", lambda: None)
    monkeypatch.setattr(startup_database, "get_engine", lambda: engine)

    db_init.prepare_database()

    artifacts = sorted((tmp_path / "backups").glob("moviepilot_*_sqlite_*.db"))
    assert len(artifacts) == 1
    with create_engine(f"sqlite:///{artifacts[0]}").connect() as connection:
        backup_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    with engine.connect() as connection:
        active_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        active_columns = {
            column["name"] for column in inspect(connection).get_columns("records")
        }

    assert backup_revision == "001"
    assert active_revision == "002"
    assert active_columns == {"id", "migrated"}


def test_migration_config_write_rolls_back_with_alembic_transaction(
        monkeypatch,
) -> None:
    """配置 DML 不得脱离 Alembic 事务提前提交。"""
    migration = importlib.import_module(
        "database.versions.e8b1c4d7a2f9_2_2_18"
    )
    engine = create_engine("sqlite://")

    metadata = MetaData()
    systemconfig = Table(
        "systemconfig",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("key", String),
        Column("value", JSON),
    )
    for table_name in ("subscribe", "subscribehistory", "transferhistory"):
        Table(table_name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    legacy_organize = """
{
    'title': '{{ title_year }}'
            '{% if season_episode %} {{ season_episode }}{% endif %} 已入库',
    'text': '{% if vote_average %}评分：{{ vote_average }}，{% endif %}'
            '类型：{{ type }}'
            '{% if category %}，类别：{{ category }}{% endif %}'
            '{% if resource_term %}，质量：{{ resource_term }}{% endif %}，'
            '共{{ file_count }}个文件，大小：{{ total_size }}'
            '{% if err_msg %}，以下文件处理失败：{{ err_msg }}{% endif %}'
}"""
    legacy_download = """
{
    'title': '{{ title_year }}'
            '{% if download_episodes %} {{ season_fmt }} {{ download_episodes }}{% else %}{{ season_episode }}{% endif %} 开始下载',
    'text': '{% if site_name %}站点：{{ site_name }}{% endif %}'
            '{% if resource_term %}\\n质量：{{ resource_term }}{% endif %}'
            '{% if size %}\\n大小：{{ size }}{% endif %}'
            '{% if torrent_title %}\\n种子：{{ torrent_title }}{% endif %}'
            '{% if pubdate %}\\n发布时间：{{ pubdate }}{% endif %}'
            '{% if freedate %}\\n免费时间：{{ freedate }}{% endif %}'
            '{% if seeders %}\\n做种数：{{ seeders }}{% endif %}'
            '{% if volume_factor %}\\n促销：{{ volume_factor }}{% endif %}'
            '{% if hit_and_run %}\\nHit&Run：{{ hit_and_run }}{% endif %}'
            '{% if labels %}\\n标签：{{ labels }}{% endif %}'
            '{% if description %}\\n描述：{{ description }}{% endif %}'
}"""
    original_templates = {
        "organizeSuccess": legacy_organize,
        "downloadAdded": legacy_download,
    }
    with engine.begin() as connection:
        connection.execute(
            systemconfig.insert().values(
                key="NotificationTemplates",
                value=original_templates,
            )
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        config_write_seen = False

        def fail_after_config_write(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
        ) -> None:
            nonlocal config_write_seen
            if statement.lstrip().upper().startswith("UPDATE SYSTEMCONFIG"):
                config_write_seen = True
                raise RuntimeError("injected migration failure")

        event.listen(engine, "after_cursor_execute", fail_after_config_write)
        try:
            with pytest.raises(RuntimeError, match="injected migration failure"):
                migration.upgrade()
        finally:
            event.remove(engine, "after_cursor_execute", fail_after_config_write)
            transaction.rollback()

        assert config_write_seen

    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("subscribe")
        }
        # SQLite 默认驱动不回滚 DDL，但配置写入仍必须服从 Alembic 事务。
        assert "audio_quality" in columns
        stored_templates = connection.execute(
            systemconfig.select().with_only_columns(systemconfig.c.value).where(
                systemconfig.c.key == "NotificationTemplates"
            )
        ).scalar_one()
        assert stored_templates == original_templates


def test_initial_migration_does_not_seed_user_and_initializes_storages(monkeypatch) -> None:
    """2.0.0 初始化只准备存储，管理员由首次访问页面创建。"""
    migration = importlib.import_module(
        "database.versions.294b007932ef_2_0_0"
    )
    engine = create_engine("sqlite://")

    metadata = MetaData()
    Table(
        "user",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String, nullable=False),
        Column("email", String),
        Column("hashed_password", String),
        Column("is_active", Boolean),
        Column("is_superuser", Boolean),
        Column("avatar", String),
        Column("is_otp", Boolean),
        Column("otp_secret", String),
        Column("permissions", JSON),
        Column("settings", JSON),
    )
    systemconfig = Table(
        "systemconfig",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("key", String),
        Column("value", JSON),
    )
    metadata.create_all(engine)

    with engine.connect() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.commit()

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM user")).scalar_one() == 0
        assert connection.execute(text("SELECT value FROM systemconfig WHERE key = 'Storages'")).scalar_one()


def test_userconfig_cleanup_migration_uses_alembic_transaction(
        monkeypatch,
) -> None:
    """2.0.3 用户配置清理必须随当前 Alembic 事务一起回滚。"""
    migration = importlib.import_module(
        "database.versions.e2dbe1421fa4_2_0_3"
    )
    engine = create_engine("sqlite://")

    metadata = MetaData()
    table_columns = {
        "downloadhistory": (("note", JSON), ("media_category", String)),
        "subscribe": (
            ("note", JSON),
            ("custom_words", String),
            ("media_category", String),
            ("filter_groups", JSON),
        ),
        "mediaserveritem": (("note", JSON),),
        "message": (("note", JSON),),
        "plugindata": (("value", JSON),),
        "site": (("note", JSON),),
        "sitestatistic": (("note", JSON),),
        "systemconfig": (("value", JSON),),
        "userconfig": (("value", JSON),),
    }
    tables = {
        table_name: Table(
            table_name,
            metadata,
            Column("id", Integer, primary_key=True),
            *(Column(column_name, column_type) for column_name, column_type in columns),
        )
        for table_name, columns in table_columns.items()
    }
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["userconfig"].insert().values(value={"retained": True})
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        assert connection.execute(
            sa.select(sa.func.count()).select_from(tables["userconfig"])
        ).scalar_one() == 0
        transaction.rollback()

    with engine.connect() as connection:
        assert connection.execute(
            sa.select(sa.func.count()).select_from(tables["userconfig"])
        ).scalar_one() == 1


def test_user_permission_migration_uses_alembic_transaction(
        monkeypatch,
) -> None:
    """2.1.6 权限初始化必须保留原筛选语义并随迁移事务回滚。"""
    migration = importlib.import_module(
        "database.versions.3df653756eec_2_1_6"
    )
    engine = create_engine("sqlite://")

    metadata = MetaData()
    user = Table(
        "user",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("is_superuser", Boolean),
        Column("permissions", JSON),
    )
    metadata.create_all(engine)
    existing_permissions = {"manage": True}
    with engine.begin() as connection:
        connection.execute(user.insert(), [
            {"id": 1, "is_superuser": False, "permissions": None},
            {"id": 2, "is_superuser": False, "permissions": existing_permissions},
            {"id": 3, "is_superuser": True, "permissions": None},
        ])

    with engine.connect() as connection:
        transaction = connection.begin()
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        migrated = connection.execute(
            sa.select(user.c.id, user.c.permissions).order_by(user.c.id)
        ).all()
        assert migrated == [
            (
                1,
                {
                    "discovery": True,
                    "search": True,
                    "subscribe": True,
                    "manage": False,
                },
            ),
            (2, existing_permissions),
            (3, None),
        ]
        transaction.rollback()

    with engine.connect() as connection:
        assert connection.execute(
            sa.select(user.c.id, user.c.permissions).order_by(user.c.id)
        ).all() == [
            (1, None),
            (2, existing_permissions),
            (3, None),
        ]


def test_local_setup_returns_failure_when_database_migration_fails(
        monkeypatch,
        capsys,
) -> None:
    """本地维护命令不得在迁移失败后继续访问业务表。"""
    module = _load_local_setup_module()
    migration_error = RuntimeError("migration failed")

    def fail_sync() -> None:
        raise migration_error

    monkeypatch.setattr(sys, "argv", [str(LOCAL_SETUP_PATH), "sync-superuser"])
    monkeypatch.setattr(module, "_resolve_interactive_config_dir", lambda *_: None)
    monkeypatch.setattr(module, "configure_config_dir", lambda **_: Path("config"))
    monkeypatch.setattr(module, "_sync_superuser_account_inner", fail_sync)

    assert module.main() == 1
    assert "migration failed" in capsys.readouterr().err


def test_local_setup_apply_config_registers_offline_transaction_runner(
        monkeypatch,
        tmp_path: Path,
        db,
) -> None:
    """离线 apply-config 写入配置前必须装配同步事务执行器。"""
    db.watermark(SystemConfig)
    module = _load_local_setup_module()
    monkeypatch.setattr(db_init, "prepare_database", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_ensure_superuser_account_inner", lambda: None)
    payload = {
        "directories": [{
            "name": "offline-config",
            "download_path": str(tmp_path / "downloads"),
            "library_path": str(tmp_path / "library"),
            "priority": 0,
        }],
    }

    module._apply_local_system_config_inner(payload)

    persisted = SystemConfig.get_by_key(
        db.session,
        "Directories",
    )
    assert persisted is not None
    assert persisted.value[0]["name"] == "offline-config"


def test_local_setup_apply_config_refreshes_stable_category_snapshot(
    monkeypatch,
    tmp_path: Path,
    db,
    isolated_classification_config,
) -> None:
    """离线 apply-config 应按活动策略保存稳定 ID 和当前分类路径快照。"""
    db.watermark(SystemConfig)
    policy = build_default_classification_policy()
    policy.categories.append(
        ClassificationCategory(
            id="tv.anime.jp",
            media_type="电视剧",
            name="日番",
            path=["动漫", "日番"],
        )
    )
    db.add(
        SystemConfig(
            key="MediaClassificationPolicy",
            value=ClassificationPolicyState(
                active=policy.model_copy(update={"revision": 1}),
            ).model_dump(mode="json"),
        )
    )
    module = _load_local_setup_module()
    monkeypatch.setattr(db_init, "prepare_database", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_ensure_superuser_account_inner", lambda: None)

    module._apply_local_system_config_inner(
        {
            "directories": [
                {
                    "name": "anime",
                    "download_path": str(tmp_path / "downloads"),
                    "media_type": "tv",
                    "media_category_id": "tv.anime.jp",
                    "media_category": "旧动漫/日番",
                }
            ]
        }
    )

    persisted = SystemConfig.get_by_key(db.session, "Directories")
    assert persisted is not None
    assert persisted.value[0]["media_type"] == "电视剧"
    assert persisted.value[0]["media_category_id"] == "tv.anime.jp"
    assert persisted.value[0]["media_category"] == "动漫/日番"


def test_local_setup_apply_config_rejects_invalid_stable_category_without_overwrite(
    monkeypatch,
    tmp_path: Path,
    db,
    isolated_classification_config,
) -> None:
    """离线目录分类 ID 无效时不得覆盖已持久化的目录配置。"""
    db.watermark(SystemConfig)
    policy = build_default_classification_policy().model_copy(
        update={"revision": 1}
    )
    db.add(
        SystemConfig(
            key="MediaClassificationPolicy",
            value=ClassificationPolicyState(active=policy).model_dump(mode="json"),
        )
    )
    db.add(
        SystemConfig(
            key="Directories",
            value=[{"name": "existing", "download_path": "/existing"}],
        )
    )
    module = _load_local_setup_module()
    monkeypatch.setattr(db_init, "prepare_database", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_ensure_superuser_account_inner", lambda: None)

    with pytest.raises(ValueError, match="分类 ID 无效"):
        module._apply_local_system_config_inner(
            {
                "directories": [
                    {
                        "name": "invalid",
                        "download_path": str(tmp_path / "downloads"),
                        "media_type": "tv",
                        "media_category_id": "tv.deleted",
                    }
                ]
            }
        )

    persisted = SystemConfig.get_by_key(db.session, "Directories")
    assert persisted is not None
    assert persisted.value == [
        {"name": "existing", "download_path": "/existing"}
    ]
