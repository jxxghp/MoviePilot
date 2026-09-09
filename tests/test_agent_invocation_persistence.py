"""Agent 写工具持久认领、重启防重和会话清理边界测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.application.invocation import InvocationConflictError, InvocationIdentity
from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.db.adapters.invocation import TransactionalInvocationRepository
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.agentchat import AgentChat
from app.db.models.agentinvocation import AgentInvocation
from app.db.models.agenttask import AgentTask
from app.db.oper.agentchat import AgentChatOper

DIGEST = sha256(b'{"operation_id":"downloads.add"}').hexdigest()
IDENTITY = InvocationIdentity("user-1", "chat-1", "call-1")


@pytest.fixture
def invocation_store(tmp_path):
    """用真实独立 SQLite 文件覆盖跨线程和重建适配器的持久行为。"""
    path = tmp_path / "invocations.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 20})
    for model in (AgentInvocation, AgentChat, AgentTask):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    yield TransactionalInvocationRepository(factory), factory, path
    engine.dispose()


def _claim(store, identity=IDENTITY):
    """为同一个宿主写工具生成稳定输入指纹。"""
    return store.claim(identity, tool_name="moviepilot_api", arguments_digest=DIGEST)


def test_concurrent_claim_grants_exactly_one_execution(invocation_store):
    """多个线程同时竞争同一身份时只有一个调用可以执行外部写入。"""
    store, _factory, _path = invocation_store
    barrier = Barrier(8)

    def compete():
        """使多个数据库连接同时尝试插入唯一身份。"""
        barrier.wait(timeout=10)
        return _claim(store)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _index: compete(), range(8)))
    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.record.claim_token for claim in claims}) == 1
    assert all(claim.record.status == "running" for claim in claims)


def test_identity_and_argument_conflicts_are_isolated(invocation_store):
    """同一调用不能换参数或工具，不同用户和会话可使用相同调用 ID。"""
    store, _factory, _path = invocation_store
    assert _claim(store).acquired
    assert not _claim(store).acquired
    with pytest.raises(InvocationConflictError):
        store.claim(IDENTITY, tool_name="moviepilot_api", arguments_digest="a" * 64)
    with pytest.raises(InvocationConflictError):
        store.claim(IDENTITY, tool_name="another_tool", arguments_digest=DIGEST)
    assert _claim(store, InvocationIdentity("user-2", "chat-1", "call-1")).acquired
    assert _claim(store, InvocationIdentity("user-1", "chat-2", "call-1")).acquired
    assert store.get(InvocationIdentity("unknown", "chat-1", "call-1")) is None


def test_restart_preserves_unknown_and_fences_previous_owner(invocation_store):
    """重启不因时间过期重放写入，旧 owner 也不能收口新核验状态。"""
    store, factory, _path = invocation_store
    original = _claim(store)
    restarted = TransactionalInvocationRepository(factory)
    assert not _claim(restarted).acquired
    assert restarted.recover_running() == 1
    unknown = restarted.get(IDENTITY)
    assert unknown.status == "unknown"
    assert unknown.claim_token != original.record.claim_token
    assert restarted.recover_running() == 0
    assert not _claim(restarted).acquired
    assert not store.finish(
        IDENTITY, claim_token=original.record.claim_token, status="succeeded",
    )
    assert restarted.finish(IDENTITY, claim_token=unknown.claim_token, status="succeeded")
    assert restarted.get(IDENTITY).status == "succeeded"
    assert not _claim(restarted).acquired
    assert not restarted.finish(IDENTITY, claim_token=unknown.claim_token, status="failed")


def test_unknown_can_be_verified_but_never_reclaimed(invocation_store):
    """执行超时只产生未知回执，核验成功前后均不能再次认领。"""
    store, _factory, _path = invocation_store
    claim = _claim(store)
    assert store.finish(IDENTITY, claim_token=claim.record.claim_token, status="unknown")
    assert not _claim(store).acquired
    assert store.finish(IDENTITY, claim_token=claim.record.claim_token, status="failed")
    assert not _claim(store).acquired


def test_pending_submission_survives_restart_without_blocking_new_intent(invocation_store):
    """已确认提交是历史回执，重启不改未知，同身份仍防重但新意图不受阻。"""
    store, factory, _path = invocation_store
    original = _claim(store)
    assert store.finish(IDENTITY, claim_token=original.record.claim_token, status="pending")
    restarted = TransactionalInvocationRepository(factory)
    assert restarted.recover_running() == 0
    record = restarted.get(IDENTITY)
    assert record.status == "pending"
    assert record.claim_token == original.record.claim_token
    assert record.summary == "写操作已确认提交，后续任务完成情况尚未观测"
    assert not _claim(restarted).acquired
    assert restarted.find_unresolved("user-1", "chat-1", tool_name="moviepilot_api", arguments_digest=DIGEST) is None
    assert _claim(restarted, InvocationIdentity("user-1", "chat-1", "new-intent")).acquired


def test_find_unresolved_respects_scope_and_ignores_terminal_receipts(invocation_store):
    """新一轮请求能查到最近旧副作用，但已收口和其他 owner 不能误阻塞。"""
    store, _factory, _path = invocation_store
    first = _claim(store)
    later_identity = InvocationIdentity("user-1", "chat-1", "later-call")
    later = _claim(store, later_identity)
    store.finish(later_identity, claim_token=later.record.claim_token, status="unknown")
    other_identity = InvocationIdentity("user-2", "chat-1", "other-call")
    _claim(store, other_identity)
    found = store.find_unresolved("user-1", "chat-1", tool_name="moviepilot_api", arguments_digest=DIGEST)
    assert found.identity == later_identity
    assert found.status == "unknown"
    assert store.find_unresolved("user-1", "chat-2", tool_name="moviepilot_api", arguments_digest=DIGEST) is None
    assert store.find_unresolved("user-1", "chat-1", tool_name="another_tool", arguments_digest=DIGEST) is None
    assert store.find_unresolved("user-1", "chat-1", tool_name="moviepilot_api", arguments_digest="b" * 64) is None
    store.finish(later_identity, claim_token=later.record.claim_token, status="succeeded")
    found = store.find_unresolved("user-1", "chat-1", tool_name="moviepilot_api", arguments_digest=DIGEST)
    assert found.identity == IDENTITY
    store.finish(IDENTITY, claim_token=first.record.claim_token, status="failed")
    assert store.find_unresolved("user-1", "chat-1", tool_name="moviepilot_api", arguments_digest=DIGEST) is None


def test_claim_rollback_does_not_grant_execution(invocation_store, monkeypatch):
    """认领事务未提交时不能返回 acquired，失败事务不会残留回执。"""
    store, _factory, _path = invocation_store

    def fail_commit(_self):
        """模拟落盘失败，使适配器退出时回滚 Session。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr("app.db.adapters.invocation.SqlAlchemyUnitOfWork.commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        _claim(store)
    assert store.get(IDENTITY) is None


@pytest.mark.parametrize("identity", [
    InvocationIdentity("", "chat-1", "call-1"),
    InvocationIdentity("user-1", "", "call-1"),
    InvocationIdentity("user-1", "chat-1", "x" * 256),
])
def test_missing_or_unbounded_identity_is_rejected(invocation_store, identity):
    """不允许匿名共享身份和无界工具调用 ID 落盘。"""
    store, _factory, _path = invocation_store
    with pytest.raises(ValueError):
        _claim(store, identity)


def test_only_digest_and_fixed_host_summary_are_persisted(invocation_store):
    """凭据原文无法通过参数指纹或自由结果摘要进入调用回执。"""
    store, factory, _path = invocation_store
    with pytest.raises(ValueError, match="SHA-256"):
        store.claim(IDENTITY, tool_name="moviepilot_api", arguments_digest="password=super-secret")
    claim = _claim(store)
    store.finish(IDENTITY, claim_token=claim.record.claim_token, status="unknown")
    with factory() as session:
        record = session.execute(select(AgentInvocation)).scalar_one()
        assert record.arguments_digest == DIGEST
        assert len(record.summary) <= 128
        assert record.summary == "写操作结果未知，必须核验状态后再决定下一步"
        assert "super-secret" not in str(record.to_dict())


def _save_chat(factory, session_id="chat-1"):
    """创建可由生命周期清理的旧会话。"""
    with factory() as session:
        chat = AgentChat(
            user_id="user-1", session_id=session_id, title="test",
            created_at="2020-01-01 00:00:00", updated_at="2020-01-01 00:00:00",
        )
        session.add(chat)
        session.commit()
        return chat.id


@pytest.mark.parametrize("receipt_status", ["succeeded", "failed", "pending"])
def test_explicit_chat_delete_removes_only_confirmed_receipts(invocation_store, receipt_status):
    """用户删除会话可清理终态和已提交回执，未知状态仍保留恢复依据。"""
    store, factory, _path = invocation_store
    chat_id = _save_chat(factory)
    terminal = _claim(store)
    store.finish(IDENTITY, claim_token=terminal.record.claim_token, status=receipt_status)
    unresolved_identity = InvocationIdentity("user-1", "chat-1", "call-unknown")
    unresolved = _claim(store, unresolved_identity)
    store.finish(unresolved_identity, claim_token=unresolved.record.claim_token, status="unknown")
    with factory.begin() as session:
        AgentChatOper(session).delete_by_id(chat_id)
    assert store.get(IDENTITY) is None
    assert store.get(unresolved_identity).status == "unknown"


@pytest.mark.asyncio
async def test_async_chat_deletion_shares_receipt_transaction(invocation_store):
    """异步请求删除与回执回收共用事务，回滚时两者均还原。"""
    store, factory, path = invocation_store
    _save_chat(factory)
    claim = _claim(store)
    store.finish(IDENTITY, claim_token=claim.record.claim_token, status="failed")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async_factory = async_sessionmaker(engine)
    try:
        async with async_factory() as session:
            assert await AgentChatOper(session).async_stage_delete("chat-1", "user-1")
            await session.rollback()
        assert store.get(IDENTITY) is not None
        async with async_factory.begin() as session:
            assert await AgentChatOper(session).async_delete("chat-1", "user-1")
        assert store.get(IDENTITY) is None
    finally:
        await engine.dispose()


def test_retention_cleanup_preserves_recovery_chats_and_receipts(invocation_store):
    """共享保留期回收已确认回执会话，未知和运行中记录不随年龄回收。"""
    store, factory, _path = invocation_store
    for name, status in (("done", "succeeded"), ("submitted", "pending"), ("unknown", "unknown"), ("running", "running")):
        _save_chat(factory, name)
        identity = InvocationIdentity("user-1", name, "call-1")
        claim = _claim(store, identity)
        if status != "running":
            store.finish(identity, claim_token=claim.record.claim_token, status=status)
    with factory.begin() as session:
        assert DatabaseCleanupRepository.delete_agent_chats(session, "2021-01-01", 10) == 2
    with factory() as session:
        assert set(session.execute(select(AgentChat.session_id)).scalars()) == {"unknown", "running"}
        assert set(session.execute(select(AgentInvocation.status)).scalars()) == {"unknown", "running"}


@pytest.mark.parametrize("enabled,retention_days,expected_deleted", [
    (True, 30, 3),
    (True, 0, 0),
    (False, 30, 0),
])
def test_orphan_confirmed_receipts_follow_shared_retention(
    invocation_store, enabled, retention_days, expected_deleted,
):
    """后台已确认提交和终态按会话保留期清理，恢复态、近期记录和禁用开关受保护。"""
    store, factory, _path = invocation_store
    old = "2026-06-01T12:00:00+00:00"
    recent = "2026-08-20T12:00:00+00:00"
    for name, status, timestamp in (
        ("old-success", "succeeded", old),
        ("old-failure", "failed", old),
        ("old-pending", "pending", old),
        ("old-unknown", "unknown", old),
        ("old-running", "running", old),
        ("recent-success", "succeeded", recent),
        ("recent-pending", "pending", recent),
        ("associated-success", "succeeded", old),
    ):
        identity = InvocationIdentity("user-1", name, "call-1")
        claim = _claim(store, identity)
        if status != "running":
            store.finish(identity, claim_token=claim.record.claim_token, status=status)
        with factory.begin() as session:
            record = session.execute(select(AgentInvocation).where(
                AgentInvocation.session_id == name,
            )).scalar_one()
            record.created_at = record.updated_at = timestamp
    with factory.begin() as session:
        session.add(AgentChat(
            user_id="user-1", session_id="associated-success", title="test",
            created_at="2026-06-01 12:00:00", updated_at="2026-08-20 12:00:00",
        ))
    policy = CleanupPolicy(
        enabled=enabled, agent_chat_days=retention_days,
        message_days=0, download_history_days=0, site_userdata_days=0,
        transfer_history_days=0, download_failure_days=0, subscribe_history_days=0,
        agent_task_run_days=0, outbox_completed_days=0, outbox_dead_days=0,
    )
    report = DataCleanupService(
        repository=DatabaseCleanupRepository(session_factory=factory),
        policy_reader=lambda: policy,
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    ).execute(batch_size=1)
    assert report["total_deleted"] == expected_deleted
    if enabled:
        assert report["tables"]["agentinvocation"]["deleted"] == expected_deleted
        assert report["tables"]["agentinvocation"]["batches"] == expected_deleted
        if retention_days == 0:
            assert report["tables"]["agentinvocation"]["skipped"] is True
    with factory() as session:
        remaining = set(session.execute(select(AgentInvocation.session_id)).scalars())
    assert {"old-unknown", "old-running", "recent-success", "recent-pending", "associated-success"} <= remaining
    assert len(remaining) == 8 - expected_deleted
