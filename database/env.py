"""
Alembic 的在线执行与离线 SQL 生成入口，装配历史迁移需要的缓存依赖。
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.db.base import Base
from app.startup.initializers.cache import configure_cache_dependencies

# 历史迁移会调用使用 FileCache 的业务清理链，Alembic 自身也是组合入口。
configure_cache_dependencies()
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 使用宿主模型元数据生成结构差异。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    按连接地址生成迁移 SQL，不建立数据库连接。

    配置字面量参数，并为 SQLite 启用批量表结构迁移；迁移中的执行语句输出到脚本。
    依赖实时反射或数据读取的历史迁移不能仅靠离线模式执行。
    """
    url = config.get_main_option("sqlalchemy.url")

    if url and "postgresql" in url:
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
    else:
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            render_as_batch=True
        )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    建立独占连接并在迁移事务中执行版本链。

    使用无连接池引擎，连接在上下文退出时关闭；SQLite 使用批量表结构迁移。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        url = config.get_main_option("sqlalchemy.url")

        if url and "postgresql" in url:
            context.configure(
                connection=connection,
                target_metadata=target_metadata
            )
        else:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True
            )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
