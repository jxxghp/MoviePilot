from collections.abc import Callable
from configparser import ConfigParser as _ConfigParser
import traceback

from alembic.command import upgrade
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.runtime.config import settings
from app.db.base import Base
from app.db.engine import get_engine
from app.db.models import load_all_models
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import configure_transaction_runners
from app.runtime.log import logger
from app.startup.bindings.database import build_database_governance
from app.startup.ports.transaction import TransactionalWriteRunner


def _configure_migration_transaction_runner() -> None:
    """在 Alembic 调用旧无会话 Oper 前装配可独立提交的兼容事务。"""
    runner = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(sync=runner.sync, async_=runner.async_)


def _build_alembic_config(engine: Engine | None = None) -> Config:
    """构造与应用活动数据库一致的 Alembic 配置。"""
    engine = engine or get_engine()
    alembic_cfg = Config()
    alembic_cfg.file_config = _ConfigParser(interpolation=None)
    alembic_cfg.set_main_option(
        'script_location',
        str(settings.ROOT_PATH / 'database'),
    )
    alembic_cfg.set_main_option(
        'sqlalchemy.url',
        engine.url.render_as_string(hide_password=False),
    )
    return alembic_cfg


def _migration_state(
        engine: Engine,
        alembic_cfg: Config,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """读取数据库迁移状态，并在结构写入前校验版本链。"""
    script = ScriptDirectory.from_config(alembic_cfg)
    target_heads = tuple(script.get_heads())
    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        current_heads = tuple(
            MigrationContext.configure(connection).get_current_heads()
        )
    has_existing_database = bool(table_names - {'alembic_version'})
    _validate_migration_lineage(script, current_heads, target_heads)
    return has_existing_database, current_heads, target_heads


def _validate_migration_lineage(
        script: ScriptDirectory,
        current_heads: tuple[str, ...],
        target_heads: tuple[str, ...],
) -> None:
    """拒绝无法沿当前迁移链安全升级的数据库版本。"""
    if len(target_heads) != 1:
        raise RuntimeError(
            f"数据库迁移脚本必须只有一个 head，当前为 {target_heads}"
        )
    if len(current_heads) > 1:
        raise RuntimeError(
            f"数据库存在多个 current revision，无法自动迁移：{current_heads}"
        )
    if not current_heads:
        return

    current = current_heads[0]
    target = target_heads[0]
    try:
        script.get_revision(current)
    except CommandError as error:
        raise RuntimeError(
            f"当前 MoviePilot 无法识别数据库 revision：{current}"
        ) from error
    if current == target:
        return

    ancestors = {
        revision.revision
        for revision in script.walk_revisions(base='base', head=target)
    }
    if current not in ancestors:
        raise RuntimeError(
            f"数据库 revision {current} 不是当前 head {target} 的可升级祖先"
        )


def prepare_database(*, before_alembic: Callable[[], None] | None = None) -> None:
    """在建表或迁移前完成版本校验及可选备份。"""
    engine = get_engine()
    alembic_cfg = _build_alembic_config(engine)
    has_existing_database, current_heads, target_heads = _migration_state(
        engine,
        alembic_cfg,
    )
    requires_migration = (
        has_existing_database
        and set(current_heads) != set(target_heads)
    )
    if (
        requires_migration
        and settings.DB_BACKUP_ENABLE
        and settings.DB_BACKUP_ON_UPGRADE
    ):
        current_version = current_heads[0] if current_heads else "未标记"
        target_version = target_heads[0]
        logger.info(
            f"数据库需要从版本 {current_version} 升级到 {target_version}，"
            "正在创建迁移前备份"
        )
        build_database_governance().create_backup()

    init_db()
    if before_alembic:
        # 首次初始化需要先建立用户表，再把管理员密码交给 Alembic 基础迁移消费。
        before_alembic()
    update_db(alembic_cfg)


def verify_database_revision() -> None:
    """确认活动数据库已位于当前唯一 Alembic head，否则阻止 readiness。"""
    engine = get_engine()
    alembic_cfg = _build_alembic_config(engine)
    _, current_heads, target_heads = _migration_state(engine, alembic_cfg)
    if set(current_heads) != set(target_heads):
        raise RuntimeError(
            "数据库迁移完成后 revision 仍未到达当前 head："
            f"current={current_heads}, target={target_heads}"
        )


def init_db():
    """
    初始化数据库
    """
    # 确保所有模型都已注册到 Base.metadata 中
    load_all_models()

    # 全量建表
    Base.metadata.create_all(bind=get_engine())


def update_db(alembic_cfg: Config | None = None):
    """
    更新数据库
    """
    try:
        # 早期迁移脚本会调用 SystemConfigOper()，此时 modules_initializer 尚未执行。
        _configure_migration_transaction_runner()
        alembic_cfg = alembic_cfg or _build_alembic_config()
        upgrade(alembic_cfg, 'head')
    except Exception as error:
        logger.error(
            f"数据库更新失败：{error}\n{traceback.format_exc()}"
        )
        raise
