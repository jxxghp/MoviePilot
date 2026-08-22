"""插件数据库容器注册表：按 (plugin_id, instance_id) 管理容器的建立、建表与释放。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional, Tuple, Type

from alembic.command import upgrade as _alembic_upgrade
from alembic.config import Config as _AlembicConfig
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker

from app.db.engine import build_sqlite_engine, get_engine
from app.db.plugin.container import PluginDatabaseHandle
from app.db.plugin.locator import postgres_schema_name, sqlite_db_path, sqlite_sidecar_paths
from app.runtime.config import settings
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.log import logger

_InstanceKey = Tuple[str, str]


class _Declaration:
    """
    插件实例声明的建库方式：模型基类与迁移目录二选一，迁移目录优先。

    普通类而非 dataclass：``app/db`` 包内的类级类型注解一律被声明式系统的
    Mapped[] 校验守卫扫描，非 ORM 的辅助类用 __init__ 赋值以避开误判。
    """

    def __init__(self) -> None:
        """初始化为两者均未声明的空状态。"""
        self.base: Optional[Type[DeclarativeBase]] = None
        self.migrations_dir: Optional[Path] = None


_lock = threading.RLock()
_declarations: Dict[_InstanceKey, _Declaration] = {}
_containers: Dict[_InstanceKey, PluginDatabaseHandle] = {}


def _is_postgresql() -> bool:
    """判断宿主当前是否使用 PostgreSQL。"""
    return settings.DB_TYPE.lower() == "postgresql"


def declare_models(plugin_id: str, instance_id: str, base: Type[DeclarativeBase]) -> None:
    """
    登记插件实例待建表的声明式基类。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    :param base: 声明式基类，其 metadata 上注册的全部表会在建库时一并建表
    """
    key = (plugin_id, instance_id)
    with _lock:
        _declarations.setdefault(key, _Declaration()).base = base


def declare_migrations(plugin_id: str, instance_id: str, directory: Path) -> None:
    """
    登记插件实例的 Alembic 迁移脚本目录，声明后建库改走 alembic upgrade 而非建表。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    :param directory: 迁移脚本目录，须符合 Alembic script_location 布局
    """
    key = (plugin_id, instance_id)
    with _lock:
        _declarations.setdefault(key, _Declaration()).migrations_dir = directory


def _build_container(plugin_id: str, instance_id: str) -> PluginDatabaseHandle:
    """
    构造插件实例的数据库容器。

    SQLite 下每实例一个独立库文件，容器独占 engine；PostgreSQL 下复用宿主引擎，
    按 execution_options(schema_translate_map=...) 派生出限定单一 schema 的外观，
    容器不拥有该 engine。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    :return: 新建的数据库容器
    """
    if _is_postgresql():
        schema = postgres_schema_name(plugin_id, instance_id)
        host_engine = get_engine()
        with host_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        engine = host_engine.execution_options(schema_translate_map={None: schema})
        db_path = None
        owns_engine = False
    else:
        db_path = sqlite_db_path(plugin_id, instance_id)
        engine = build_sqlite_engine(f"sqlite:///{db_path}")
        schema = None
        owns_engine = True

    session_factory = sessionmaker(bind=engine)
    return PluginDatabaseHandle(
        plugin_id=plugin_id,
        instance_id=instance_id,
        engine=engine,
        session_factory=session_factory,
        scoped_session_factory=scoped_session(session_factory),
        metadata=None,
        db_path=db_path,
        schema=schema,
        owns_engine=owns_engine,
    )


def _get_or_build_container(plugin_id: str, instance_id: str) -> PluginDatabaseHandle:
    """取已建立的容器，不存在时按需建立并缓存。"""
    key = (plugin_id, instance_id)
    with _lock:
        container = _containers.get(key)
        if container is None:
            container = _build_container(plugin_id, instance_id)
            _containers[key] = container
        return container


def get_database(plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> PluginDatabaseHandle:
    """
    取插件实例的数据库句柄，容器不存在时按需建立。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识，本批固定取默认实例
    :return: 数据库容器
    """
    return _get_or_build_container(plugin_id, instance_id)


def _run_migrations(container: PluginDatabaseHandle, directory: Path) -> None:
    """
    在容器对应的数据库上把 Alembic 迁移跑到 head。

    SQLite 直接按库文件 URL 起一个独立连接跑迁移；PostgreSQL 复用容器已经
    schema_translate_map 限定过的连接，迁移脚本的 env.py 须从
    ``context.config.attributes["connection"]`` 取用该连接，而不是自行按 URL
    建连接，否则迁移会落在 public schema 而非本插件实例的 schema。
    :param container: 数据库容器
    :param directory: 迁移脚本目录
    """
    cfg = _AlembicConfig()
    cfg.set_main_option("script_location", str(directory))
    if container.owns_engine:
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{container.db_path}")
        _alembic_upgrade(cfg, "head")
    else:
        with container.engine.connect() as connection:
            cfg.attributes["connection"] = connection
            _alembic_upgrade(cfg, "head")
            connection.commit()


def ensure_database(plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> None:
    """
    按插件实例的声明建立数据库：声明了迁移目录走 alembic，否则按声明的模型建表。

    未声明模型也未声明迁移目录时不做任何事，不建立容器、不产生任何库文件。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识，本批固定取默认实例
    """
    key = (plugin_id, instance_id)
    with _lock:
        declaration = _declarations.get(key)
    if declaration is None:
        return
    if declaration.migrations_dir is not None:
        container = _get_or_build_container(plugin_id, instance_id)
        _run_migrations(container, declaration.migrations_dir)
        return
    if declaration.base is not None and declaration.base.metadata.tables:
        container = _get_or_build_container(plugin_id, instance_id)
        container.metadata = declaration.base.metadata
        declaration.base.metadata.create_all(bind=container.engine)


def release_instance(plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> None:
    """
    释放插件实例的数据库连接。

    只 dispose 容器自身拥有的 engine；PostgreSQL 场景下容器只是宿主引擎的外观，
    这里不会、也不能触碰宿主连接池。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    """
    key = (plugin_id, instance_id)
    with _lock:
        container = _containers.pop(key, None)
    if container is not None:
        container.dispose()


def release_plugin(plugin_id: str) -> None:
    """
    释放某插件全部实例的数据库连接。
    :param plugin_id: 插件标识
    """
    with _lock:
        keys = [key for key in _containers if key[0] == plugin_id]
    for key in keys:
        release_instance(*key)


def release_all() -> None:
    """释放全部插件实例的数据库连接，用于进程关停。"""
    with _lock:
        keys = list(_containers)
    for key in keys:
        release_instance(*key)


def destroy_database(plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> None:
    """
    销毁插件实例的数据库：SQLite 删除库文件及 -wal/-shm 边车文件，PostgreSQL 丢弃
    对应 schema。

    不可逆操作，调用方须确认处在「删除插件数据」而非「停止插件」的路径上。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识
    """
    key = (plugin_id, instance_id)
    with _lock:
        container = _containers.pop(key, None)
        _declarations.pop(key, None)
    if container is not None:
        container.dispose()

    if _is_postgresql():
        schema = container.schema if container else postgres_schema_name(plugin_id, instance_id)
        try:
            with get_engine().begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception as error:  # noqa: BLE001
            logger.error(f"销毁插件[{plugin_id}]数据库 schema {schema} 失败：{error}")
        return

    db_path = container.db_path if container else sqlite_db_path(plugin_id, instance_id)
    for candidate in (db_path,) + sqlite_sidecar_paths(db_path):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            logger.warning(f"删除插件数据库文件 {candidate} 失败：{error}")
