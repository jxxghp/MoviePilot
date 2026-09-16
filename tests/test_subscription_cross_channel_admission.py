"""订阅 Search/Match 公共入口的跨通道准入测试。"""

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context
from app.schemas.types import MediaType


def _subscribe(subscribe_id: int) -> SubscriptionSnapshot:
    """构造已越过新增保护期的活动电影订阅。"""
    return SubscriptionSnapshot(
        id=subscribe_id,
        name=f"并发电影 {subscribe_id}",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source="themoviedb",
        media_id=str(5000 + subscribe_id),
        state="R",
        date=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _repository(*subscribes: SubscriptionSnapshot) -> SimpleNamespace:
    """提供公共入口重新读取所需的稳定订阅仓储。"""
    snapshots = {subscribe.id: subscribe for subscribe in subscribes}
    return SimpleNamespace(
        get=snapshots.get,
        list=lambda _state: list(snapshots.values()),
    )


def _configure_channel_state(monkeypatch) -> None:
    """为不同 SubscribeChain 实例安装进程级共享准入与独立通道锁。"""
    monkeypatch.setattr(SubscribeChain, "_match_lock", threading.Lock())
    monkeypatch.setattr(SubscribeChain, "_search_queue_lock", threading.Lock())
    monkeypatch.setattr(
        SubscribeChain,
        "_subscription_execution_admission",
        SubscriptionExecutionAdmission(),
    )


def test_public_search_and_match_skip_same_subscription_across_instances(monkeypatch) -> None:
    """两个独立 Chain 实例处理同一订阅时只能有一个进入业务路径。"""
    _configure_channel_state(monkeypatch)
    subscribe = _subscribe(7)
    repository = _repository(subscribe)
    search_chain = object.__new__(SubscribeChain)
    search_chain.subscription_repository = repository
    match_chain = object.__new__(SubscribeChain)
    match_chain.subscription_repository = repository
    match_chain.get_states_for_search = lambda state: state
    match_chain._prepare_match_torrents = lambda torrents: torrents

    search_started = threading.Event()
    release_search = threading.Event()

    def process_search(item, _searchchain, *, execution_context):
        """持有 Search 准入，直到 Match 完成冲突探测。"""
        assert execution_context.lease.subscription_id == item.id
        search_started.set()
        assert release_search.wait(timeout=3)
        return item

    match_subscription = Mock(side_effect=AssertionError("冲突 Match 不得进入订阅业务路径"))
    search_chain._process_search_subscription = process_search
    match_chain._match_subscription = match_subscription
    worker = threading.Thread(
        target=search_chain.search,
        kwargs={"sid": subscribe.id, "state": None},
    )

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        worker.start()
        assert search_started.wait(timeout=3)
        match_chain.match({"example.org": [Context()]})
        release_search.set()
        worker.join(timeout=3)

    assert worker.is_alive() is False
    match_subscription.assert_not_called()


def test_public_search_and_match_allow_different_subscriptions_in_parallel(monkeypatch) -> None:
    """Search 持有一条订阅时 Match 仍可处理另一条订阅。"""
    _configure_channel_state(monkeypatch)
    search_subscribe = _subscribe(8)
    match_subscribe = _subscribe(9)
    search_chain = object.__new__(SubscribeChain)
    search_chain.subscription_repository = _repository(search_subscribe)
    match_chain = object.__new__(SubscribeChain)
    match_chain.subscription_repository = _repository(match_subscribe)
    match_chain.get_states_for_search = lambda state: state
    match_chain._prepare_match_torrents = lambda torrents: torrents

    search_started = threading.Event()
    release_search = threading.Event()

    def process_search(item, _searchchain, *, execution_context):
        """阻塞 Search 以证明 Match 不依赖 Search 通道锁。"""
        assert execution_context.lease.subscription_id == item.id
        search_started.set()
        assert release_search.wait(timeout=3)
        return item

    match_subscription = Mock()
    search_chain._process_search_subscription = process_search
    match_chain._match_subscription = match_subscription
    worker = threading.Thread(
        target=search_chain.search,
        kwargs={"sid": search_subscribe.id, "state": None},
    )

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        worker.start()
        assert search_started.wait(timeout=3)
        match_chain.match({"example.org": [Context()]})
        release_search.set()
        worker.join(timeout=3)

    assert worker.is_alive() is False
    match_subscription.assert_called_once()
    assert match_subscription.call_args.kwargs["subscribe"].id == match_subscribe.id
