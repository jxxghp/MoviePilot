"""用户冻结快照与短事务适配器测试。"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.application.security.user import AuxiliaryUserCreate
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.user import User
from app.db.uow import SqlAlchemyUnitOfWork


@pytest.fixture
def user_repository(tmp_path):
    """构造同步和异步共享同一 SQLite 文件的用户仓储。"""
    database_path = tmp_path / "users.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    User.__table__.create(sync_engine)
    sync_factory = sessionmaker(bind=sync_engine)

    @asynccontextmanager
    async def async_session():
        """生成一个测试独占的异步会话。"""
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async_factory = async_sessionmaker(bind=async_engine)
        try:
            async with async_factory() as session:
                yield session
        finally:
            await async_engine.dispose()

    repository = TransactionalUserRepository(
        sync_session=sync_factory,
        async_session=async_session,
    )
    yield repository, sync_factory
    sync_engine.dispose()


def _insert_user(sync_factory, **overrides) -> int:
    """直接写入测试用户并返回主键。"""
    values = {
        "name": "alice",
        "email": "alice@example.com",
        "hashed_password": "hash",
        "is_active": True,
        "is_superuser": True,
        "avatar": "avatar",
        "is_otp": True,
        "otp_secret": "secret",
        "permissions": {"features": {"search": True}},
        "settings": {"telegram_userid": "42", "targets": ["telegram"]},
    }
    values.update(overrides)
    with sync_factory() as session:
        user = User(**values)
        session.add(user)
        session.commit()
        return user.id


@pytest.mark.asyncio
async def test_user_snapshots_are_detached_and_deeply_frozen(user_repository) -> None:
    """会话关闭后公开与认证快照仍可读，嵌套 JSON 不可被调用方修改。"""
    repository, sync_factory = user_repository
    user_id = _insert_user(sync_factory)

    public = repository.get_by_id(user_id)
    auth = repository.get_auth_by_name("alice")
    async_public = await repository.async_get_by_name("alice")

    assert public is not None
    assert auth is not None
    assert async_public == public
    assert auth.user == public
    assert auth.hashed_password == "hash"
    assert auth.otp_secret == "secret"
    assert public.settings["targets"] == ("telegram",)
    with pytest.raises(TypeError):
        public.settings["telegram_userid"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        public.permissions["features"]["search"] = False  # type: ignore[index]


def test_auxiliary_create_commits_before_return(user_repository) -> None:
    """辅助认证创建成功返回时，新用户必须已对后续独立会话可见。"""
    repository, sync_factory = user_repository

    created = repository.create_auxiliary(AuxiliaryUserCreate(
        name="created",
        hashed_password="hash",
    ))

    assert created.name == "created"
    with sync_factory() as session:
        persisted = session.execute(
            select(User).where(User.name == "created")
        ).scalar_one()
        assert persisted.is_active is True
        assert persisted.is_superuser is False


def test_auxiliary_create_rolls_back_commit_failure(
    user_repository,
    monkeypatch,
) -> None:
    """提交异常不得留下仅 flush 成功的辅助认证用户。"""
    repository, sync_factory = user_repository

    def fail_commit(_unit_of_work) -> None:
        """模拟数据库提交阶段失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        repository.create_auxiliary(AuxiliaryUserCreate(
            name="rolled-back",
            hashed_password="hash",
        ))

    with sync_factory() as session:
        assert session.execute(
            select(User).where(User.name == "rolled-back")
        ).scalar_one_or_none() is None


def test_channel_binding_requires_one_active_unambiguous_owner(user_repository) -> None:
    """停用用户与重复渠道绑定都必须拒绝用户归属。"""
    repository, sync_factory = user_repository
    _insert_user(sync_factory, name="active", settings={"telegram_userid": "42"})
    _insert_user(
        sync_factory,
        name="disabled",
        is_active=False,
        settings={"telegram_userid": "77"},
    )

    assert repository.find_name_by_bindings({"telegram_userid": 42}) == "active"
    assert repository.find_name_by_bindings({"telegram_userid": 77}) is None

    _insert_user(sync_factory, name="conflict", settings={"telegram_userid": "42"})
    assert repository.find_name_by_bindings({"telegram_userid": 42}) is None


def test_channel_binding_requires_all_supplied_identifiers(user_repository) -> None:
    """多标识渠道只有全部标识指向同一用户时才允许归属。"""
    repository, sync_factory = user_repository
    _insert_user(
        sync_factory,
        name="feishu-user",
        settings={"feishu_userid": "u-1", "feishu_openid": "o-1"},
    )

    assert repository.find_name_by_bindings({
        "feishu_userid": "u-1",
        "feishu_openid": "o-1",
    }) == "feishu-user"
    assert repository.find_name_by_bindings({
        "feishu_userid": "u-1",
        "feishu_openid": "wrong",
    }) is None
