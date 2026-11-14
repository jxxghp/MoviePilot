"""
使用临时 SQLite 验证用户同步更新的短事务、回滚和执行器作用域。
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.adapters.transaction import TransactionalWriteRunner
from app.db.models.user import User
from app.db.oper.user import UserOper
from app.db.uow import transaction_runner_scope


@pytest.fixture
def user_database_factory(tmp_path):
    """
    按名称创建互不共享数据的临时用户表及真实事务执行器。
    """
    engines = []

    def create_database(name):
        """
        为一个作用域创建数据库，异步入口按调用释放自己的引擎。
        """
        path = tmp_path / f"{name}.db"
        engine = create_engine(f"sqlite:///{path}")
        engines.append(engine)
        User.__table__.create(engine)
        factory = sessionmaker(bind=engine)

        @asynccontextmanager
        async def async_session():
            """
            为同一数据库提供可独立关闭的真实异步会话。
            """
            async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with async_sessionmaker(bind=async_engine)() as session:
                    yield session
            finally:
                await async_engine.dispose()

        return factory, TransactionalWriteRunner(
            sync_session=factory,
            async_session=async_session,
        )

    try:
        yield create_database
    finally:
        for engine in engines:
            engine.dispose()


def _insert_user(factory, name="admin"):
    """
    提交一个启用的管理员，返回关闭会话后仍有效的主键。
    """
    with factory() as session:
        user = User(
            name=name,
            email="before@example.invalid",
            hashed_password="old-hash",
            is_active=True,
            is_superuser=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
        session.commit()
        return user_id


def test_update_commits_fields_after_previous_query_session_closes(user_database_factory):
    """
    更新重新查询已游离的管理员，提交全部字段并返回可读取的对象。
    """
    factory, runner = user_database_factory("persist")
    user_id = _insert_user(factory)
    payload = {
        "email": "after@example.invalid",
        "hashed_password": "new-hash",
        "is_active": False,
        "is_superuser": False,
        "permissions": {"search": True},
        "settings": {"language": "zh"},
    }

    with transaction_runner_scope(sync=runner.sync, async_=runner.async_):
        oper = UserOper()
        previous = oper.get_by_id(user_id)
        assert previous is not None
        assert inspect(previous).detached

        updated = oper.update(user_id, payload)

    assert updated is not None
    assert updated is not previous
    assert inspect(updated).detached
    assert updated.id == user_id
    assert previous.email == "before@example.invalid"
    for field, value in payload.items():
        assert getattr(updated, field) == value
    with factory() as session:
        persisted = session.get(User, user_id)
        assert persisted is not None
        for field, value in payload.items():
            assert getattr(persisted, field) == value


def test_update_returns_none_when_previously_queried_user_was_deleted(user_database_factory):
    """
    查询后已删除的用户不会被更新入口重新插入。
    """
    factory, runner = user_database_factory("missing")
    user_id = _insert_user(factory)

    with transaction_runner_scope(sync=runner.sync, async_=runner.async_):
        oper = UserOper()
        assert oper.get_by_id(user_id) is not None
        with factory() as session:
            session.delete(session.get(User, user_id))
            session.commit()

        assert oper.update(user_id, {"hashed_password": "new-hash"}) is None
        assert oper.list() == []

    with factory() as session:
        assert session.get(User, user_id) is None


def test_update_failure_rolls_back_and_restores_previous_runners(user_database_factory):
    """
    唯一约束失败回滚全部字段，退出嵌套作用域后恢复同步和异步执行器。
    """
    outer_factory, outer_runner = user_database_factory("outer")
    inner_factory, inner_runner = user_database_factory("inner")
    outer_id = _insert_user(outer_factory, "outer-admin")
    inner_id = _insert_user(inner_factory, "inner-admin")
    _insert_user(inner_factory, "occupied-name")

    with transaction_runner_scope(sync=outer_runner.sync, async_=outer_runner.async_):
        with pytest.raises(IntegrityError):
            with transaction_runner_scope(sync=inner_runner.sync, async_=inner_runner.async_):
                UserOper().update(inner_id, {
                    "name": "occupied-name",
                    "email": "must-rollback@example.invalid",
                    "hashed_password": "must-rollback",
                })

        with inner_factory() as session:
            persisted = session.get(User, inner_id)
            assert persisted is not None
            assert persisted.name == "inner-admin"
            assert persisted.email == "before@example.invalid"
            assert persisted.hashed_password == "old-hash"

        restored = UserOper().update(outer_id, {"email": "restored@example.invalid"})
        assert restored is not None
        assert restored.name == "outer-admin"
        async_user = asyncio.run(UserOper().async_get_by_name("outer-admin"))
        assert async_user is not None
        assert async_user.email == "restored@example.invalid"

    with outer_factory() as session:
        assert session.get(User, outer_id).email == "restored@example.invalid"
    with inner_factory() as session:
        assert session.get(User, inner_id).email == "before@example.invalid"
