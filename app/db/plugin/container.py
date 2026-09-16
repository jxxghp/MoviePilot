"""插件数据库句柄：持有引擎、会话工厂与释放策略。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

__all__ = ["PluginDatabaseHandle"]


@dataclass(frozen=True, slots=True)
class PluginDatabaseHandle:
    """
    一个插件专属数据库的连接要素。

    SQLite 下引擎由本句柄独占，``owns_engine`` 为真；PostgreSQL 下引擎是宿主引擎按
    ``schema_translate_map`` 派生的外观，``owns_engine`` 为假——只有前者可以 dispose，
    后者一旦 dispose 会连累宿主与其它插件仍在使用的同一个连接池。PostgreSQL 下本句柄的
    会话与连接在每个事务开始时把 ``search_path`` 限定到插件 schema 本身，未限定的原生 SQL
    因此同样解析到插件自己的表，找不到的表名直接报错而不会落到宿主同名表。
    """

    plugin_id: str
    engine: Engine
    session_factory: sessionmaker
    scoped_session_factory: scoped_session
    db_path: Path | None
    schema: str | None
    owns_engine: bool

    def session(self) -> Session:
        """新建一个绑定本库的会话，提交、回滚与关闭由调用方负责。"""
        return self.session_factory()

    def scoped_session(self) -> Session:
        """取当前线程绑定的会话，同一线程内重复调用复用同一个实例。"""
        return self.scoped_session_factory()

    def dispose(self) -> None:
        """释放本句柄独占的连接池；不拥有引擎时不做任何事。"""
        if self.owns_engine:
            self.engine.dispose()
