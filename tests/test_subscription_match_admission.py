"""订阅 Match 通道的逐订阅准入与异常隔离测试。"""

import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.chain.subscribe import match as subscribe_match
from app.chain.subscribe.facade import SubscribeChain
from app.schemas.types import MediaType


def _subscribe(subscribe_id: int, *, name: str | None = None) -> SubscriptionSnapshot:
    """构造 Match 编排所需的活动电影订阅快照。"""
    return SubscriptionSnapshot(
        id=subscribe_id,
        name=name or f"匹配电影 {subscribe_id}",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source="themoviedb",
        media_id=str(2000 + subscribe_id),
        state="R",
    )


def _chain(
    listed: list[SubscriptionSnapshot],
    current: dict[int, SubscriptionSnapshot] | None = None,
) -> SubscribeChain:
    """构造只执行 Match 编排层的订阅链。"""
    snapshots = current or {subscribe.id: subscribe for subscribe in listed}
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(
        list=lambda _state: list(listed),
        get=snapshots.get,
    )
    chain.get_states_for_search = lambda state: state
    chain._match_lock = threading.Lock()
    chain._search_queue_lock = threading.Lock()
    chain._subscription_execution_admission = SubscriptionExecutionAdmission()
    chain._prepare_match_torrents = lambda torrents: torrents
    return chain


def _assert_channel_and_subscription_released(
    chain: SubscribeChain,
    subscription_ids: tuple[int, ...],
) -> None:
    """验证 Match 通道锁和所有订阅 owner 均已释放。"""
    assert chain._match_lock.acquire(blocking=False) is True
    chain._match_lock.release()
    for subscription_id in subscription_ids:
        lease = chain._subscription_execution_admission.try_acquire(
            subscription_id=subscription_id,
            operation="search",
            ttl_seconds=60,
        )
        assert lease is not None
        assert chain._subscription_execution_admission.release(lease) is True


def test_match_skips_subscription_owned_by_search() -> None:
    """Search 已持有同一订阅时 Match 本轮直接跳过。"""
    subscribe = _subscribe(1)
    chain = _chain([subscribe])
    process = Mock(side_effect=AssertionError("冲突订阅不应进入 Match"))
    chain._match_subscription = process
    search_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="search",
        ttl_seconds=60,
    )
    assert search_lease is not None

    chain.match({"example.org": []})

    process.assert_not_called()
    assert chain._match_lock.acquire(blocking=False) is True
    chain._match_lock.release()
    assert chain._subscription_execution_admission.release(search_lease) is True


def test_match_reloads_subscription_after_admission() -> None:
    """Match 取得 owner 后使用最新订阅快照并传入独立执行上下文。"""
    listed = _subscribe(2, name="旧快照")
    current = replace(listed, name="最新快照")
    chain = _chain([listed], {listed.id: current})
    captured = []

    def process(**kwargs):
        """记录 Match 单订阅执行入口收到的快照与上下文。"""
        captured.append(kwargs)

    chain._match_subscription = process

    chain.match({"example.org": []})

    assert len(captured) == 1
    assert captured[0]["subscribe"] is current
    context = captured[0]["execution_context"]
    assert context.lease.subscription_id == listed.id
    assert context.lease.operation == "match"
    _assert_channel_and_subscription_released(chain, (listed.id,))


def test_match_isolates_one_subscription_failure_and_releases_all_owners() -> None:
    """单条 Match 异常不阻止后续订阅，且所有 owner 最终释放。"""
    first = _subscribe(3)
    second = _subscribe(4)
    chain = _chain([first, second])
    processed = []

    def process(**kwargs):
        """让首条订阅失败并记录后续订阅仍被执行。"""
        subscribe = kwargs["subscribe"]
        processed.append(subscribe.id)
        if subscribe.id == first.id:
            raise RuntimeError("candidate failure")

    chain._match_subscription = process

    chain.match({"example.org": []})

    assert processed == [first.id, second.id]
    _assert_channel_and_subscription_released(chain, (first.id, second.id))


def test_match_continues_when_latest_subscription_read_fails() -> None:
    """单条最新快照读取失败不能泄漏 owner 或中止后续 Match。"""
    first = _subscribe(5)
    second = _subscribe(6)
    chain = _chain([first, second])
    processed = []

    def get(subscription_id: int):
        """模拟首条读取异常并返回第二条当前快照。"""
        if subscription_id == first.id:
            raise RuntimeError("repository failure")
        return second

    chain.subscription_repository.get = get
    chain._match_subscription = lambda **kwargs: processed.append(kwargs["subscribe"].id)

    chain.match({"example.org": []})

    assert processed == [second.id]
    _assert_channel_and_subscription_released(chain, (first.id, second.id))


def test_match_skips_subscription_paused_after_admission() -> None:
    """取得准入后订阅变为暂停态时，不应继续访问候选或下载边界。"""
    listed = _subscribe(7)
    paused = replace(listed, state="S")
    chain = _chain([listed], {listed.id: paused})
    process = Mock(side_effect=AssertionError("暂停订阅不应进入 Match"))
    chain._match_subscription = process
    progress = Mock()

    chain.match({"example.org": []}, progress_callback=progress)

    process.assert_not_called()
    final_data = progress.call_args.kwargs["data"]
    assert final_data == {
        "total": 1,
        "finished": 1,
        "completed": 0,
        "skipped": 1,
        "failed": 0,
    }
    assert progress.call_args.kwargs["text"] == "订阅资源匹配完成，部分订阅跳过"
    _assert_channel_and_subscription_released(chain, (listed.id,))


def test_match_progress_reports_completed_skipped_and_failed_counts() -> None:
    """Match 最终进度必须区分正常执行、准入跳过和单订阅失败。"""
    completed = _subscribe(8)
    skipped = _subscribe(9)
    failed = _subscribe(10)
    chain = _chain([completed, skipped, failed])
    chain._match_subscription = Mock(
        side_effect=["completed", RuntimeError("candidate failure")]
    )
    skipped_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=skipped.id,
        operation="search",
        ttl_seconds=60,
    )
    assert skipped_lease is not None
    progress = Mock()

    chain.match({"example.org": []}, progress_callback=progress)

    final_data = progress.call_args.kwargs["data"]
    assert final_data == {
        "total": 3,
        "finished": 3,
        "completed": 1,
        "skipped": 1,
        "failed": 1,
    }
    assert progress.call_args.kwargs["text"] == "订阅资源匹配完成，部分订阅失败"
    assert chain._subscription_execution_admission.release(skipped_lease) is True
    _assert_channel_and_subscription_released(chain, (completed.id, failed.id))


def test_match_stop_does_not_report_unvisited_subscriptions_as_completed(monkeypatch) -> None:
    """系统停止后未访问的订阅不得计入完成或跳过。"""
    subscribes = [_subscribe(11), _subscribe(12)]
    chain = _chain(subscribes)
    progress = Mock()
    monkeypatch.setattr(
        subscribe_match,
        "runtime_stop_state",
        SimpleNamespace(is_system_stopped=True),
    )

    chain.match({"example.org": []}, progress_callback=progress)

    assert progress.call_args.kwargs == {
        "value": 100,
        "text": "订阅资源匹配已停止，部分订阅未执行",
        "data": {
            "total": 2,
            "finished": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
        },
    }
