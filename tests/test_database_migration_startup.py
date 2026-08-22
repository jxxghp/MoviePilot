import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from alembic.util import CommandError
from fastapi import FastAPI
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine.url import make_url

from app.startup import database_initializer as db_init
from app.startup.bindings import database as startup_database
from app.startup import lifecycle
from app.runtime.health import get_application_health


LOCAL_SETUP_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"
)


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

    assert calls == ["backup", "create_all", "before_alembic", "alembic"]
    assert logged_messages == [
        "数据库需要从版本 old 升级到 head，正在创建迁移前备份"
    ]


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

    artifacts = sorted((tmp_path / "backups").glob("sqlite_*.db"))
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
