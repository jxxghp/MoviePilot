"""
用户短事务写入的测试作用域；不注册测试用例。
"""

from contextlib import asynccontextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.engine import _register_sqlite_foreign_keys
from app.db.models.passkey import PassKey
from app.db.models.user import User
from app.db.models.userconfig import UserConfig


@asynccontextmanager
async def _user_write_context(database_path):
    """
    创建包含用户聚合表的异步请求会话。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sync_engine = create_engine(f"sqlite:///{database_path}")
    _register_sqlite_foreign_keys(engine.sync_engine)
    _register_sqlite_foreign_keys(sync_engine)
    sync_factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(UserConfig.__table__.create)
        await connection.run_sync(PassKey.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session, sync_factory
    finally:
        await engine.dispose()
        sync_engine.dispose()
