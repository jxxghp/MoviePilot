"""插件认证一次性票据的生命周期测试。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.application.security import auth
from app.application.security.auth import AuthTicketStore


@pytest.fixture(autouse=True)
def clear_auth_tickets():
    """隔离单例票据缓存，避免测试间共享认证事实。"""
    store = AuthTicketStore()
    with store._lock:
        store._tickets.clear()
    yield
    with store._lock:
        store._tickets.clear()


def test_auth_ticket_can_only_be_consumed_once():
    """成功领取后立即删除票据，后续兑换不得重复获得认证事实。"""
    store = AuthTicketStore()
    ticket = store.create(user_id=1, provider_id="plugin:test")

    assert store.consume(ticket)["user_id"] == 1
    assert store.consume(ticket) is None


def test_auth_ticket_concurrent_consumers_have_single_winner():
    """多个并发兑换请求只能有一个成功领取同一票据。"""
    store = AuthTicketStore()
    ticket = store.create(user_id=1, provider_id="plugin:test")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: store.consume(ticket), range(8)))

    assert sum(result is not None for result in results) == 1


def test_auth_ticket_ttl_boundary_and_expiration(monkeypatch):
    """票据在 TTL 边界内有效，超过边界后即使首次领取也必须失败。"""
    now = [1_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    store = AuthTicketStore()

    boundary_ticket = store.create(user_id=1, provider_id="plugin:test")
    now[0] += store._ttl_seconds
    assert store.consume(boundary_ticket) is not None

    expired_ticket = store.create(user_id=1, provider_id="plugin:test")
    now[0] += store._ttl_seconds + 0.001
    assert store.consume(expired_ticket) is None
    assert store.consume(expired_ticket) is None


def test_auth_ticket_metadata_is_detached_from_callers():
    """签发和领取两侧都不能通过可变对象改写缓存中的认证元数据。"""
    store = AuthTicketStore()
    metadata = {"groups": ["users"]}
    ticket = store.create(
        user_id=1,
        provider_id="plugin:test",
        metadata=metadata,
    )
    metadata["groups"].append("admins")

    consumed = store.consume(ticket)

    assert consumed["metadata"] == {"groups": ["users"]}


def test_auth_ticket_capacity_is_a_hard_limit(monkeypatch):
    """连续签发也不得让票据表永久超过容量上限。"""
    monkeypatch.setattr(AuthTicketStore, "_max_items", 4)
    store = AuthTicketStore()

    tickets = [
        store.create(user_id=index, provider_id="plugin:test")
        for index in range(6)
    ]

    assert len(store._tickets) == 4
    assert store.consume(tickets[0]) is None
    assert store.consume(tickets[-1]) is not None
