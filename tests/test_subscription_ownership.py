"""
媒体订阅旧归属的迁移、身份隔离与管理员复核。
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.security.access import oauth2_scheme_manual_error
from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.subscription import get_subscription_ownership_command
from app.api.endpoints.subscribe import router
from app.application.subscription.complete import CompleteSubscriptionCommand
from app.application.subscription.ownership import (
    ConfirmSubscriptionOwner,
    SubscriptionOwnerAccount,
    subscription_owner_revision,
)
from app.application.subscription.query import SubscriptionQueryService
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionOwnershipRepository,
    SessionSubscriptionRepository,
)
from app.db.models.subscribe import Subscribe, subscription_identity_key
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.session import async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork


def _new_item(**values):
    """
    创建拥有完整仲裁键的样例订阅。
    """
    return Subscribe(**{
        "name": "示例剧", "username": "old-name", "type": "电视剧", "media_source": "themoviedb",
        "media_id": "42", "season": 1, "state": "R", **values,
    })


def test_ownership_dependency_uses_current_session_and_transaction():
    """
    归属复核命令由当前请求的仓储和事务装配，缺少仓储时拒绝继续。
    """
    session = object()
    unit_of_work = object()
    repositories = []

    def new_repository(scoped_session):
        """
        记录本次请求的会话。
        """
        repositories.append(scoped_session)
        return object()

    runtime = SimpleNamespace(ownership_repository=new_repository)
    assert isinstance(get_subscription_ownership_command(session, runtime, unit_of_work), ConfirmSubscriptionOwner)
    assert repositories == [session]
    runtime.ownership_repository = None
    with pytest.raises(RuntimeError, match="归属确认仓储未注册"):
        get_subscription_ownership_command(session, runtime, unit_of_work)


@pytest.mark.asyncio
async def test_review_retains_unclaimed_records_and_uses_stable_owner_after_rename(db):
    """
    未确认记录不给同名账号，改名后的原账号仍可读写和按页计数。
    """
    member = db.add(User(name="old-name", is_active=True))
    other = db.add(User(name="other", is_active=True))
    legacy = db.add(_new_item())
    history = db.add(SubscribeHistory(name="旧历史", username="old-name", type="电视剧"))
    assert legacy.identity_key and not legacy.owner_identity
    legacy_id, history_id, member_id = legacy.id, history.id, member.id
    owner, other_owner = member.notification_identity, other.notification_identity

    async with async_session_scope() as session:
        repository = SessionSubscriptionRepository(session)
        history_repository = SessionSubscriptionHistoryRepository(session)
        query = SubscriptionQueryService(repository, async_repository=repository, history_repository=history_repository)
        command = ConfirmSubscriptionOwner(SessionSubscriptionOwnershipRepository(session), SqlAlchemyAsyncUnitOfWork(session))
        assert await query.list_public("old-name", page=1, count=1, owner_identity=owner) == []
        assert await query.count_public("old-name", owner_identity=owner) == 0
        assert await query.count_history("电视剧", username="old-name", owner_identity=owner) == 0
        row = await query.get_public(legacy_id)
        account = next(item for item in await command.list_accounts() if item.id == member_id)
        assert row is not None and row.ownership_revision
        assert await command.execute("active", legacy_id, row.ownership_revision, member_id, account.revision, 1) == "confirmed"
        assert await command.execute("active", legacy_id, row.ownership_revision, member_id, account.revision, 1) == "changed"
        legacy_history = await history_repository.async_get(history_id)
        assert legacy_history is not None
        assert await command.execute(
            "history", history_id, subscription_owner_revision(legacy_history), member_id, account.revision, 1,
        ) == "confirmed"

    member.name = "new-name"
    db.session.commit()
    db.add(User(name="old-name", is_active=True))
    async with async_session_scope() as session:
        repository = SessionSubscriptionRepository(session)
        query = SubscriptionQueryService(
            repository, async_repository=repository, history_repository=SessionSubscriptionHistoryRepository(session),
        )
        records = await query.list_public("new-name", page=1, count=1, owner_identity=owner)
        assert [item.id for item in records] == [legacy_id]
        assert not records[0].ownership_pending
        assert "owner_identity" not in records[0].model_dump()
        assert await query.count_public("other", owner_identity=other_owner) == 0
        assert await query.count_history("电视剧", username="new-name", owner_identity=owner) == 1
        assert await repository.list_search_ids("new-name", "R", owner_identity=owner) == [legacy_id]
        db_row = await session.get(Subscribe, legacy_id)
        assert db_row.identity_key == subscription_identity_key(db_row.__dict__)


@pytest.mark.asyncio
async def test_confirmation_rejects_account_reuse_and_duplicate_subscription(db):
    """
    确认前同名重建会使复核版本失效，重复媒体身份保持两条原记录。
    """
    member = db.add(User(name="member", is_active=True))
    member_id, prior_owner = member.id, member.notification_identity
    legacy = db.add(_new_item())
    legacy_id = legacy.id
    async with async_session_scope() as session:
        repository = SessionSubscriptionRepository(session)
        record = await repository.async_get(legacy_id)
        revision = subscription_owner_revision(record)
        owners = SessionSubscriptionOwnershipRepository(session)
        account = next(item for item in await owners.list_accounts() if item.id == member_id)
    db.session.delete(member)
    db.session.commit()
    replacement = db.add(User(id=member_id, name="member", is_active=True))
    assert replacement.notification_identity != prior_owner
    db.add(_new_item(username="member", owner_identity=replacement.notification_identity))

    async with async_session_scope() as session:
        owners = SessionSubscriptionOwnershipRepository(session)
        command = ConfirmSubscriptionOwner(owners, SqlAlchemyAsyncUnitOfWork(session))
        assert await command.execute("active", legacy_id, revision, member_id, account.revision, 1) == "invalid_account"
        replacement_account = next(item for item in await owners.list_accounts() if item.id == member_id)
        assert await command.execute("active", legacy_id, revision, member_id, replacement_account.revision, 1) == "changed"
        assert (await session.get(Subscribe, legacy_id)).owner_identity is None



@pytest.mark.asyncio
async def test_simultaneous_reviews_do_not_overwrite_owner(db):
    """
    两位管理员复核同一旧记录时只提交一次归属。
    """
    record_id = db.add(_new_item()).id
    for name in ("recipient-a", "recipient-b"):
        db.add(User(name=name, is_active=True))
    async with async_session_scope() as session:
        record = await SessionSubscriptionRepository(session).async_get(record_id)
        revision = subscription_owner_revision(record)
        accounts = await SessionSubscriptionOwnershipRepository(session).list_accounts()

    async def claim(account):
        """
        为每个管理员使用独立事务。
        """
        async with async_session_scope() as session:
            command = ConfirmSubscriptionOwner(
                SessionSubscriptionOwnershipRepository(session), SqlAlchemyAsyncUnitOfWork(session),
            )
            return await command.execute("active", record_id, revision, account.id, account.revision, account.id)

    assert sorted(await asyncio.gather(*(claim(account) for account in accounts))) == ["changed", "confirmed"]


def test_completion_carries_latest_confirmed_owner_into_history(db):
    """
    完成任务持有旧快照时，事务内重新读取归属并用同一账号投递。
    """
    subscription = db.add(_new_item())
    repository = SessionSubscriptionRepository(db.session)
    stale = repository.get(subscription.id)
    subscription.owner_identity = "a" * 32
    db.session.commit()
    used = []
    command = CompleteSubscriptionCommand(
        repository, SqlAlchemyUnitOfWork(db.session), None, None, lambda _event: None,
        lock_record=repository.lock_for_completion,
    )
    command.execute(
        stale.id, stale.to_dict(), {}, lambda: pytest.fail("缺少完成归属"),
        lambda _payload: True, notify_owned=used.append,
    )
    assert used == ["a" * 32]
    history = db.session.scalar(sa.select(SubscribeHistory).where(SubscribeHistory.activity_key == stale.activity_key))
    assert history.owner_identity == "a" * 32


def test_completion_ignores_subscription_removed_after_snapshot(db):
    """
    完成前重新锁定记录，已被删除的订阅不会生成历史或发送通知。
    """
    subscription = db.add(_new_item())
    repository = SessionSubscriptionRepository(db.session)
    stale = repository.get(subscription.id)
    db.session.delete(subscription)
    db.session.commit()

    def unexpected(*_args):
        """
        结束已删除订阅不触发业务副作用。
        """
        pytest.fail("已删除记录产生了完成副作用")

    command = CompleteSubscriptionCommand(
        repository, SqlAlchemyUnitOfWork(db.session), None, None, unexpected,
        lock_record=repository.lock_for_completion,
    )
    command.execute(stale.id, stale.to_dict(), {}, unexpected, unexpected)
    assert not db.session.scalar(sa.select(SubscribeHistory).where(SubscribeHistory.activity_key == stale.activity_key))


def test_completion_rolls_back_when_ownership_lock_fails(db):
    """
    获取当前归属失败时回滚，保留活动订阅且不生成历史或对外通知。
    """
    subscription = db.add(_new_item())
    repository = SessionSubscriptionRepository(db.session)
    snapshot = repository.get(subscription.id)
    transaction = Mock(wraps=SqlAlchemyUnitOfWork(db.session))
    effect = Mock()
    lock_record = Mock(side_effect=RuntimeError("ownership lock failed"))
    command = CompleteSubscriptionCommand(
        repository, transaction, None, None, effect, lock_record=lock_record,
    )
    with pytest.raises(RuntimeError, match="ownership lock failed"):
        command.execute(snapshot.id, snapshot.to_dict(), {}, effect, effect)
    transaction.rollback.assert_called_once()
    transaction.commit.assert_not_called()
    effect.assert_not_called()
    assert repository.get(snapshot.id) is not None
    assert not db.session.scalar(sa.select(SubscribeHistory).where(SubscribeHistory.activity_key == snapshot.activity_key))


def test_admin_endpoint_rejects_other_principals():
    """
    仅接受网页管理员，提交的账号版本和记录版本逐字段校验。
    """
    app = FastAPI()
    app.include_router(router)
    actor = SimpleNamespace(id=8, is_superuser=True, account_type="local", authorization_id=None)
    calls = []

    async def execute(*args):
        calls.append(args)
        return "confirmed"

    app.dependency_overrides[get_current_active_user_async] = lambda: actor
    app.dependency_overrides[oauth2_scheme_manual_error] = lambda: None
    async def list_accounts():
        """
        提供稳定顺序的三条复核选项。
        """
        return [SubscriptionOwnerAccount(account_id, f"account-{account_id}", "a" * 64)
                for account_id in range(1, 4)]

    app.dependency_overrides[get_subscription_ownership_command] = lambda: SimpleNamespace(
        execute=execute, list_accounts=list_accounts,
    )
    body = {"account_id": 7, "revision": "a" * 64, "account_revision": "b" * 64}
    with TestClient(app) as client:
        assert client.put("/ownership/active/123", json=body).status_code == 403
        app.dependency_overrides[oauth2_scheme_manual_error] = lambda: "validated-by-auth-dependency"
        actor.authorization_id = "linked-channel"
        assert client.put("/ownership/active/123", json=body).status_code == 403
        actor.authorization_id = None
        actor.is_superuser = False
        assert client.put("/ownership/active/123", json=body).status_code == 403
        actor.is_superuser = True
        assert client.put("/ownership/history/123", json={**body, "owner_identity": "forged"}).status_code == 422
        assert client.put("/ownership/history/123", json=body).json()["success"]
        result = client.get("/ownership/accounts?page=2&count=1")
        assert result.status_code == 200
        assert result.headers["X-Total-Count"] == "3"
        assert result.headers["X-Result-Count"] == "1"
        assert [item["id"] for item in result.json()["data"]] == [2]
    assert calls == [("history", 123, "a" * 64, 7, "b" * 64, 8)]


def test_migration_retains_legacy_name_and_refuses_to_drop_ownership(monkeypatch):
    """
    升级只添加归属列，已确认记录需通过备份恢复。
    """
    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    with sa.create_engine("sqlite://").begin() as connection:
        for name in ("subscribe", "subscribehistory"):
            connection.exec_driver_sql(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, username VARCHAR)")
            connection.exec_driver_sql(f"INSERT INTO {name} VALUES (1, 'legacy')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration._add_subscription_ownership()
        migration._add_subscription_ownership()
        assert connection.exec_driver_sql("SELECT username, owner_identity FROM subscribe").one() == ("legacy", None)
        connection.exec_driver_sql("UPDATE subscribehistory SET owner_identity = 'approved'")
        with pytest.raises(RuntimeError, match="备份回退"):
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT owner_identity FROM subscribehistory").scalar_one() == "approved"
