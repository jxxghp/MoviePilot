"""
用户凭据、并发管理员保护与设置类型的审查回归。
"""

import asyncio
import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.endpoints.user import create_user, upload_avatar
from app.application.github_auth import GithubAuthService
from app.application.security.throttle import LoginThrottle
from app.application.security.user import LastActiveSuperuserError
from app.application.settings.service import SystemSettingsService
from app.db.adapters.configuration import TransactionalUserConfigurationRepository
from app.db.adapters.user import SqlAlchemyUserRepository
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.oper.userconfig import UserConfigOper
from app.domain.github import GithubAuthTransportError, GithubTokenExchange, GithubUser
from app.schemas.initialization import InitializationRequest
from app.schemas.types import SystemConfigKey
from app.schemas.user import UserCreate
from tests.test_user_repository import _user_write_context


@pytest.mark.asyncio
async def test_avatar_contains_decodable_base64() -> None:
    """
    头像 data URL 只携带 Base64 文本，不包含 bytes 的 Python 表示。
    """
    import base64

    service = SimpleNamespace(get_by_id=AsyncMock(return_value=object()), update=AsyncMock())
    file = UploadFile(filename="avatar.ico", file=BytesIO(b"review-image"))
    response = await upload_avatar(7, service, file, SimpleNamespace(id=7, is_superuser=False))
    assert response.success
    encoded = service.update.call_args.args[1]["avatar"].split(",", 1)[1]
    assert base64.b64decode(encoded, validate=True) == b"review-image"


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, ""])
async def test_create_user_removes_empty_optional_password(password) -> None:
    """
    可选密码为空时移除明文字段，创建无本地密码用户仍符合合同。
    """
    service = SimpleNamespace(create=AsyncMock(return_value=object()))
    response = await create_user(service=service, user_in=UserCreate(name="review", password=password))
    assert response.success
    assert "password" not in service.create.call_args.args[0]
    assert "hashed_password" not in service.create.call_args.args[0]


@pytest.mark.parametrize("field,value", [("username", "   "), ("api_key", " " * 16)])
def test_initialization_rejects_blank_after_normalization(field, value) -> None:
    """
    首尾空白在长度约束之前去除，空账号和空令牌不能完成初始化。
    """
    payload = {"username": "review", "password": "Test123!", "confirm_password": "Test123!",
               "api_key": "review-api-token-12345678", field: value}
    with pytest.raises(ValidationError):
        InitializationRequest.model_validate(payload)


def test_login_failure_ttl_resets_same_key() -> None:
    """
    同一用户隔超时再次失败从首次计数开始，未过期锁仍按预算保留。
    """
    now = [0.0]
    throttle = LoginThrottle(max_failures=2, ttl_seconds=10, clock=lambda: now[0])
    key = ("127.0.0.1", "review")
    assert throttle.record_failure(key) == 0
    now[0] = 11
    assert throttle.retry_after(key) == 0
    assert throttle.record_failure(key) == 0
    assert throttle.record_failure(key) == 30
    now[0] = 22
    assert throttle.retry_after(key) == 19


@pytest.mark.asyncio
async def test_settings_persists_type_adapter_result() -> None:
    """
    合同接受的站点数字文本持久化为整数，消费者按站点主键匹配。
    """
    saved = {}
    async def write(key, value, **_kwargs):
        """
        在内存端口记录已规范化候选，保留真实服务的类型处理。
        """
        saved[key] = value
        return SimpleNamespace(normalized_value=value, changed=True)
    config = SimpleNamespace(get=lambda key: saved.get(key), async_set_with_normalized_value=write)
    service = SystemSettingsService(None, config, AsyncMock())
    await service.update(setting_key="IndexerSites", value=["1", "2"])
    assert saved[SystemConfigKey.IndexerSites] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["demote", "disable", "delete"])
async def test_concurrent_sqlite_admin_changes_keep_one(tmp_path, mutation) -> None:
    """
    两个独占 SQLite 请求同时移除不同管理员时，后一个事务必须看到最新人数。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
        async with AsyncSession(engine) as session:
            session.add_all([User(name="a", is_active=True, is_superuser=True),
                             User(name="b", is_active=True, is_superuser=True)])
            await session.commit()
        first_flushed, release_first = asyncio.Event(), asyncio.Event()
        async def change(user_id, first=False):
            """
            首事务在提交前暂停，确保次事务遇到仍未落账的管理员修改。
            """
            async with AsyncSession(engine, expire_on_commit=False) as session:
                repository = SqlAlchemyUserRepository(session)
                try:
                    if mutation == "delete":
                        await repository.async_delete(user_id)
                    else:
                        await repository.async_update(user_id, {
                            "is_superuser" if mutation == "demote" else "is_active": False})
                    if first:
                        first_flushed.set()
                        await release_first.wait()
                    await session.commit()
                    return "changed"
                except LastActiveSuperuserError:
                    await session.rollback()
                    return "protected"
        first = asyncio.create_task(change(1, first=True))
        await asyncio.wait_for(first_flushed.wait(), 5)
        second = asyncio.create_task(change(2))
        try:
            # 次事务必须等首事务提交，或在首事务完成后以新快照拒绝。
            await asyncio.sleep(0.05)
        finally:
            release_first.set()
        assert sorted(await asyncio.wait_for(asyncio.gather(first, second), 5)) == ["changed", "protected"]
        async with AsyncSession(engine) as session:
            remaining = (await session.execute(select(User).where(
                User.is_active.is_(True), User.is_superuser.is_(True)))).scalars().all()
            assert len(remaining) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_userconfig_waits_for_user_delete_before_reading(monkeypatch, tmp_path) -> None:
    """
    删除已暂存但未提交时，配置写入先等数据库写锁，不能先读旧行再过期更新。
    """
    async with _user_write_context(tmp_path / "delete-config.db") as (session, sync_factory):
        user = User(name="old", is_active=True, is_superuser=False)
        session.add_all([user, UserConfig(username="old", key="theme", value="initial")])
        await session.commit()
        snapshot = object.__new__(UserConfigOper)
        snapshot.__init__()
        configuration = TransactionalUserConfigurationRepository(sync_factory, snapshot)
        configuration.load_snapshot()
        await SqlAlchemyUserRepository(session).async_delete(user.id)
        attempted, read_entered = threading.Event(), threading.Event()
        original_read = UserConfig.get_by_key

        def record_read(**kwargs):
            """
            观察真实查询入口，保留查询和持久化算法。
            """
            read_entered.set()
            return original_read(**kwargs)

        def before_sql(_connection, _cursor, _statement, _parameters, _context, _executemany):
            """
            在独立同步连接第一次尝试执行 SQL 时唤醒测试。
            """
            attempted.set()

        monkeypatch.setattr(UserConfig, "get_by_key", record_read)
        bind = sync_factory.kw["bind"]
        event.listen(bind, "before_cursor_execute", before_sql)
        writing = asyncio.create_task(asyncio.to_thread(configuration.set, "old", "theme", "concurrent"))
        try:
            assert await asyncio.to_thread(attempted.wait, 5)
            assert not read_entered.is_set()
        finally:
            await session.commit()
            outcomes = await asyncio.wait_for(asyncio.gather(writing, return_exceptions=True), 5)
            event.remove(bind, "before_cursor_execute", before_sql)
        assert len(outcomes) == 1 and isinstance(outcomes[0], IntegrityError), outcomes
        with sync_factory() as verify:
            assert verify.execute(select(UserConfig)).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clear", "manual"])
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_github_refresh_does_not_restore_replaced_credentials(action, refresh_fails) -> None:
    """
    远端刷新等待期间清除或更换凭据，迟到结果不能恢复旧账号授权。
    """
    settings = Mock()
    token = ["old-access"]
    metadata = [{"login": "old-user", "expires_at": 1000, "refresh_token": "old-refresh"}]
    entered, release = asyncio.Event(), asyncio.Event()
    settings.get.side_effect = lambda _key: token[0]
    def update(_key, value):
        """
        更新内存部署设置，避免写入真实配置。
        """
        token[0] = value
        return True, ""
    settings.update.side_effect = update
    config = Mock()
    config.get.side_effect = lambda _key: dict(metadata[0]) if metadata[0] else None
    async def set_config(_key, value):
        """
        保存独立的 OAuth 元数据快照。
        """
        metadata[0] = dict(value)
    async def delete_config(_key):
        """
        删除内存 OAuth 快照。
        """
        metadata[0] = None
    config.async_set = set_config
    config.async_delete = delete_config
    async def refresh(*_args):
        """
        在远端响应边界制造可控并发窗口。
        """
        entered.set()
        await release.wait()
        if refresh_fails:
            raise GithubAuthTransportError("隔离刷新失败")
        return GithubTokenExchange(access_token="stale-refreshed", refresh_token="rotated", expires_in=3600)
    transport = SimpleNamespace(refresh_access_token=refresh,
                                get_user=AsyncMock(return_value=GithubUser(login="manual-user")))
    service = GithubAuthService(settings=settings, system_config=config, transport=transport,
                                client_id="review", clock=lambda: 1000)
    refreshing = asyncio.create_task(service.access_token())
    await asyncio.wait_for(entered.wait(), 5)
    try:
        if action == "clear":
            await service.clear_token()
        else:
            await service.set_manual_token("new-manual")
    finally:
        release.set()
    result = await asyncio.wait_for(refreshing, 5)
    assert token[0] == (None if action == "clear" else "new-manual")
    assert result == ("" if action == "clear" else "new-manual")
    assert metadata[0] is None
