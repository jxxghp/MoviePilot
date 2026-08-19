"""插件专属数据库容器：持有引擎、会话工厂与按所有权决定的释放策略。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm import scoped_session as ScopedSessionRegistry


class PluginDatabaseHandle:
    """
    插件某一实例的数据库句柄。

    SQLite 场景下 engine 由本容器独占构造，``owns_engine`` 为真；PostgreSQL 场景下
    engine 是宿主引擎按 ``schema_translate_map`` 派生的外观，``owns_engine`` 为假——
    释放时只有前者需要、也只有前者可以 dispose 连接池，后者一旦 dispose 会连累宿主
    的全部连接。

    普通类而非 dataclass：``app/db`` 包内的类级类型注解一律被声明式系统的
    Mapped[] 校验守卫扫描，非 ORM 的辅助类用 __init__ 赋值以避开误判。
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        instance_id: str,
        engine: Engine,
        session_factory: sessionmaker,
        scoped_session_factory: ScopedSessionRegistry,
        metadata: Optional[MetaData],
        db_path: Optional[Path],
        schema: Optional[str],
        owns_engine: bool,
    ) -> None:
        """
        保存插件实例的数据库连接要素。
        :param plugin_id: 插件标识
        :param instance_id: 插件实例标识
        :param engine: 数据库引擎；PostgreSQL 下为宿主引擎按 schema 派生的外观
        :param session_factory: 同步会话工厂
        :param scoped_session_factory: 线程局部会话注册表
        :param metadata: 插件声明的模型所在的 MetaData；未声明模型时为 None
        :param db_path: SQLite 库文件路径；PostgreSQL 下为 None
        :param schema: PostgreSQL schema 名；SQLite 下为 None
        :param owns_engine: 本容器是否拥有 engine，决定释放时是否可以 dispose
        """
        self.plugin_id = plugin_id
        self.instance_id = instance_id
        self.engine = engine
        self.session_factory = session_factory
        self.scoped_session_factory = scoped_session_factory
        self.metadata = metadata
        self.db_path = db_path
        self.schema = schema
        self.owns_engine = owns_engine

    def session(self) -> Session:
        """
        新建一个绑定到本容器引擎的会话。
        :return: 新会话，调用方负责提交、回滚与关闭
        """
        return self.session_factory()

    def scoped_session(self) -> Session:
        """
        取当前线程的会话，同一线程内的重复调用复用同一个会话实例。
        :return: 当前线程绑定的会话
        """
        return self.scoped_session_factory()

    def dispose(self) -> None:
        """
        释放连接池。

        仅当本容器拥有 engine 时才真正 dispose；PostgreSQL 场景下 engine 只是宿主
        引擎的外观，dispose 它等同于 dispose 宿主连接池，必须跳过。
        """
        if self.owns_engine:
            self.engine.dispose()
