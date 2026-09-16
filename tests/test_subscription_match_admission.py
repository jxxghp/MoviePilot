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
    assert progress.call_args.kwargs["text"] == "订阅资源检查完成，部分订阅这次未检查"
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
    assert progress.call_args.kwargs["text"] == "订阅资源检查结束，部分订阅没有完成"
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
        "text": "订阅资源检查已停止，部分订阅这次未检查",
        "data": {
            "total": 2,
            "finished": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
        },
    }


def test_match_logs_one_bounded_start_and_finish_summary(monkeypatch) -> None:
    """Match INFO 只保留轮次摘要，并明确任务完成不等于订阅成功。"""
    completed = _subscribe(13)
    conflicted = _subscribe(14)
    cancelled = _subscribe(15)
    chain = _chain([completed, conflicted, cancelled])
    search_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=conflicted.id,
        operation="search",
        ttl_seconds=60,
    )
    assert search_lease is not None

    def process(**kwargs):
        """让一条正常处理完成，另一条在安全边界取消。"""
        if kwargs["subscribe"].id == cancelled.id:
            raise subscribe_match.SubscriptionSearchCancelled("cancelled")
        return "completed"

    chain._match_subscription = process
    info_logs = []
    monkeypatch.setattr(subscribe_match.logger, "info", info_logs.append)

    chain.match({"site-a.example": [object()], "site-b.example": [object(), object()]})

    assert len(info_logs) == 2
    assert info_logs[0] == "开始检查订阅资源，共 3 个订阅、3 个资源，来自 2 个站点。"
    assert info_logs[1].startswith("订阅资源检查结束：部分订阅这次未检查。")
    assert "本次检查 3/3 个订阅，完成 1 个，这次未检查 2 个，失败 0 个" in info_logs[1]
    assert "其中 1 个订阅正在处理中，本次没有重复检查" in info_logs[1]
    assert "另有 1 个订阅已停止" in info_logs[1]
    assert "run_id" not in info_logs[1]
    assert "订阅成功" not in "\n".join(info_logs)
    assert chain._subscription_execution_admission.release(search_lease) is True


def test_match_summary_distinguishes_ttl_timeout(monkeypatch) -> None:
    """Match 安全边界因 TTL 停止时必须单独计数。"""
    subscribe = _subscribe(16)
    chain = _chain([subscribe])
    now = [0.0]
    chain._subscription_execution_admission = SubscriptionExecutionAdmission(
        clock=lambda: now[0]
    )

    def expire(**_kwargs):
        """让当前 owner 在单订阅执行返回前超过 TTL。"""
        now[0] = chain._SUBSCRIPTION_EXECUTION_TTL + 1
        return "skipped"

    chain._match_subscription = expire
    info_logs = []
    monkeypatch.setattr(subscribe_match.logger, "info", info_logs.append)

    chain.match({"site.example": [object()]})

    assert len(info_logs) == 2
    assert "这次未检查 1 个" in info_logs[1]
    assert "另有 1 个订阅检查时间过长，已停止" in info_logs[1]


def test_match_logs_subscription_admission_release_failure(monkeypatch) -> None:
    """订阅 owner token 无法释放时必须同时进入错误日志和轮次摘要。"""
    class _ReleaseRejectingAdmission(SubscriptionExecutionAdmission):
        """模拟当前 owner 已被替换的释放冲突。"""

        def release(self, lease):
            """拒绝释放以触发可见性合同。"""
            del lease
            return False

    subscribe = _subscribe(17)
    chain = _chain([subscribe])
    chain._subscription_execution_admission = _ReleaseRejectingAdmission()
    chain._match_subscription = lambda **_kwargs: "completed"
    info_logs = []
    error_logs = []
    monkeypatch.setattr(subscribe_match.logger, "info", info_logs.append)
    monkeypatch.setattr(
        subscribe_match.logger,
        "error",
        lambda message, **_kwargs: error_logs.append(message),
    )

    chain.match({"site.example": [object()]})

    assert any("订阅 17 的搜索状态没有正常恢复，系统稍后会继续检查" in item for item in error_logs)
    assert "另有 1 个订阅的搜索状态没有正常恢复" in info_logs[-1]
