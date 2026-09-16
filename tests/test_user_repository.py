"""用户冻结快照与短事务适配器测试。"""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.application.security.user import (
    AuxiliaryUserCreate,
    LastActiveSuperuserError,
    UserNameConflictError,
    UserService,
)
from app.application.security.userconfig import UserConfigurationService
from app.db.adapters.configuration import TransactionalUserConfigurationRepository
from app.db.adapters.user import SqlAlchemyUserRepository, TransactionalUserRepository
from app.db.engine import _register_sqlite_foreign_keys
from app.db.models.passkey import PassKey
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.oper.userconfig import UserConfigOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.foundation.singleton import Singleton


class _InlineDatabaseExecutor:
    """在测试协程内执行短同步配置发布。"""

    async def run(self, operation):
        """执行并返回同步操作结果。"""
        return operation()


def _configuration_service(sync_factory) -> UserConfigurationService:
    """从当前测试数据库加载并返回用户配置发布服务。"""
    Singleton._instances.pop((UserConfigOper, (), frozenset()), None)
    snapshot = UserConfigOper()
    repository = TransactionalUserConfigurationRepository(sync_factory, snapshot)
    repository.load_snapshot()
    return UserConfigurationService(
        repository,
        async_executor=_InlineDatabaseExecutor(),
    )


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
async def test_user_repository_paginates_and_counts_in_database(tmp_path) -> None:
    """用户列表应按主键稳定分页，并以独立 COUNT 返回完整总数。"""
    async with _user_write_context(tmp_path / "pagination.db") as (session, _):
        users = [
            User(name=f"page-user-{index}", email=f"{index}@example.com")
            for index in range(1, 4)
        ]
        session.add_all(users)
        await session.commit()
        repository = SqlAlchemyUserRepository(session)

        page = await repository.async_list(page=2, count=1)

        assert await repository.async_count() == 3
        assert [item.id for item in page] == [users[1].id]
        assert users[0].id < users[1].id < users[2].id


@asynccontextmanager
async def _user_write_context(database_path):
    """创建包含用户聚合表的异步请求会话。"""
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


def test_user_repository_selects_first_active_superuser(user_repository) -> None:
    """升级绑定只能选择启用管理员，并按主键保持确定顺序。"""
    repository, sync_factory = user_repository
    _insert_user(sync_factory, name="disabled-admin", is_active=False)
    expected_id = _insert_user(sync_factory, name="first-admin")
    _insert_user(sync_factory, name="second-admin")
    _insert_user(sync_factory, name="member", is_superuser=False)

    selected = repository.get_active_superuser()

    assert selected is not None
    assert selected.id == expected_id
    assert selected.name == "first-admin"


def test_auxiliary_create_commits_before_return(user_repository) -> None:
    """辅助认证创建成功返回时，新用户必须已对后续独立会话可见。"""
    repository, sync_factory = user_repository

    created = repository.create_auxiliary(
        AuxiliaryUserCreate(
            name="created",
            hashed_password="hash",
        )
    )

    assert created.name == "created"
    with sync_factory() as session:
        persisted = session.execute(select(User).where(User.name == "created")).scalar_one()
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
        repository.create_auxiliary(
            AuxiliaryUserCreate(
                name="rolled-back",
                hashed_password="hash",
            )
        )

    with sync_factory() as session:
        assert session.execute(select(User).where(User.name == "rolled-back")).scalar_one_or_none() is None


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

    assert (
        repository.find_name_by_bindings(
            {
                "feishu_userid": "u-1",
                "feishu_openid": "o-1",
            }
        )
        == "feishu-user"
    )
    assert (
        repository.find_name_by_bindings(
            {
                "feishu_userid": "u-1",
                "feishu_openid": "wrong",
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_user_rename_migrates_configuration_atomically(tmp_path) -> None:
    """改名必须通过数据库级联迁移旧偏好。"""
    async with _user_write_context(tmp_path / "rename.db") as (session, sync_factory):
        user = User(name="old", is_active=True, is_superuser=False)
        session.add_all(
            [
                user,
                UserConfig(username="old", key="theme", value="dark"),
            ]
        )
        await session.commit()
        configuration = _configuration_service(sync_factory)
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            configuration,
        )

        renamed = await service.update(user.id, {"name": "new"})

        assert renamed is not None
        assert renamed.name == "new"
        configs = (await session.execute(select(UserConfig))).scalars().all()
        assert [(item.username, item.key, item.value) for item in configs] == [("new", "theme", "dark")]
        assert configuration.get("old", "theme") is None
        assert configuration.get("new", "theme") == "dark"


@pytest.mark.asyncio
async def test_user_delete_removes_configuration_and_passkeys(tmp_path) -> None:
    """删除用户必须在同一事务中清理字符串偏好和外键凭据。"""
    async with _user_write_context(tmp_path / "delete.db") as (session, sync_factory):
        user = User(name="member", is_active=True, is_superuser=False)
        session.add(user)
        await session.flush()
        session.add_all(
            [
                UserConfig(username="member", key="theme", value="dark"),
                PassKey(
                    user_id=user.id,
                    credential_id="credential",
                    public_key="public-key",
                ),
            ]
        )
        await session.commit()
        configuration = _configuration_service(sync_factory)
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            configuration,
        )

        await service.delete(user.id)

        assert (await session.execute(select(User))).scalar_one_or_none() is None
        assert (await session.execute(select(UserConfig))).scalar_one_or_none() is None
        assert (await session.execute(select(PassKey))).scalar_one_or_none() is None
        assert configuration.get("member", "theme") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"is_active": False}, {"is_superuser": False}])
async def test_last_active_superuser_cannot_be_disabled_or_demoted(
    tmp_path,
    payload,
) -> None:
    """更新最后一个启用管理员时必须拒绝停用和降权。"""
    async with _user_write_context(tmp_path / "last-admin-update.db") as (session, sync_factory):
        admin = User(name="admin", is_active=True, is_superuser=True)
        session.add(admin)
        await session.commit()
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            _configuration_service(sync_factory),
        )

        with pytest.raises(LastActiveSuperuserError):
            await service.update(admin.id, payload)

        persisted = (await session.execute(select(User))).scalar_one()
        assert persisted.is_active is True
        assert persisted.is_superuser is True


@pytest.mark.asyncio
async def test_last_active_superuser_cannot_be_deleted(tmp_path) -> None:
    """删除最后一个启用管理员必须完整回滚。"""
    async with _user_write_context(tmp_path / "last-admin-delete.db") as (session, sync_factory):
        admin = User(name="admin", is_active=True, is_superuser=True)
        session.add(admin)
        await session.commit()
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            _configuration_service(sync_factory),
        )

        with pytest.raises(LastActiveSuperuserError):
            await service.delete(admin.id)

        assert (await session.execute(select(User))).scalar_one().name == "admin"


@pytest.mark.asyncio
async def test_superuser_can_be_deleted_when_another_active_admin_remains(tmp_path) -> None:
    """存在另一个启用管理员时允许删除目标管理员。"""
    async with _user_write_context(tmp_path / "multiple-admins.db") as (session, sync_factory):
        first = User(name="first", is_active=True, is_superuser=True)
        second = User(name="second", is_active=True, is_superuser=True)
        session.add_all([first, second])
        await session.commit()
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            _configuration_service(sync_factory),
        )

        await service.delete(first.id)

        names = (await session.execute(select(User.name))).scalars().all()
        assert names == ["second"]


@pytest.mark.asyncio
async def test_database_unique_constraint_is_mapped_to_application_error(tmp_path) -> None:
    """并发前置检查失效后，数据库唯一约束仍返回稳定应用错误。"""
    async with _user_write_context(tmp_path / "duplicate.db") as (session, sync_factory):
        session.add(User(name="duplicate", is_active=True, is_superuser=False))
        await session.commit()
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            _configuration_service(sync_factory),
        )

        with pytest.raises(UserNameConflictError):
            await service.create({"name": "duplicate"})

        users = (await session.execute(select(User))).scalars().all()
        assert [item.name for item in users] == ["duplicate"]


async def _race_user_mutation_and_config_set(
    *,
    database_path,
    mutation: str,
    set_username: str,
) -> tuple[list[UserConfig], object]:
    """并发执行用户身份变更和配置写入并返回最终数据库、快照。"""
    async with _user_write_context(database_path) as (session, sync_factory):
        user = User(name="old", is_active=True, is_superuser=False)
        session.add_all(
            [
                user,
                UserConfig(username="old", key="theme", value="initial"),
            ]
        )
        await session.commit()
        configuration = _configuration_service(sync_factory)
        service = UserService(
            SqlAlchemyUserRepository(session),
            SqlAlchemyAsyncUnitOfWork(session),
            configuration,
        )
        barrier = threading.Barrier(2)

        def set_config() -> None:
            """与用户事务同时尝试提交配置。"""
            barrier.wait()
            configuration.set(set_username, "theme", "concurrent")

        async def mutate_user() -> None:
            """与配置事务同时提交改名或删除。"""
            await asyncio.to_thread(barrier.wait)
            if mutation == "rename":
                await service.update(user.id, {"name": "new"})
            else:
                await service.delete(user.id)

        results = await asyncio.gather(
            asyncio.to_thread(set_config),
            mutate_user(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                assert isinstance(result, IntegrityError)
        session.expire_all()
        rows = list((await session.execute(select(UserConfig))).scalars().all())
        snapshot = configuration.get(
            "new" if mutation == "rename" else "old",
            "theme",
        )
        return rows, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("set_username", ["old", "new"])
async def test_user_rename_and_config_set_are_consistent(
    tmp_path,
    set_username,
) -> None:
    """改名与旧名/新名配置真并发后不得产生孤儿或覆盖已提交值。"""
    rows, snapshot = await _race_user_mutation_and_config_set(
        database_path=tmp_path / f"rename-set-{set_username}.db",
        mutation="rename",
        set_username=set_username,
    )

    assert {row.username for row in rows} == {"new"}
    assert len(rows) == 1
    assert snapshot == rows[0].value


@pytest.mark.asyncio
async def test_user_delete_and_config_set_cannot_recreate_orphan(tmp_path) -> None:
    """删除与配置真并发后数据库和快照都不得重建无主体配置。"""
    rows, snapshot = await _race_user_mutation_and_config_set(
        database_path=tmp_path / "delete-set.db",
        mutation="delete",
        set_username="old",
    )

    assert rows == []
    assert snapshot is None
