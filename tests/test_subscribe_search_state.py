import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.subscription.contract import SubscriptionPatch, SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.application.subscription.mutation import SubscriptionMutation
from app.chain.subscribe import search as subscribe_search
from app.chain.subscribe.facade import SubscribeChain
from app.schemas.types import MediaType


class _SubscribeOper:
    """
    最小订阅 Oper 替身，隔离订阅搜索状态流转测试的数据库访问。
    """

    subscribe = None
    updates = []

    def get(self, sid: int):
        """
        按 ID 返回测试订阅对象。
        """
        return self.subscribe if self.subscribe and self.subscribe.id == sid else None

    def list(self, _state: str):
        """
        返回批量搜索需要的测试订阅列表。
        """
        return [self.subscribe] if self.subscribe else []

    def update(self, sid: int, payload: SubscriptionPatch) -> SubscriptionSnapshot:
        """
        记录订阅状态更新请求。
        """
        self.updates.append((sid, payload))
        return replace(self.subscribe, **payload.to_payload())


class _TimedOutLock:
    """模拟订阅搜索锁在等待窗口内始终无法取得。"""

    def acquire(self, **_kwargs):
        """返回未取得锁，验证调用方不会越过互斥边界继续执行。"""
        return False

    def release(self):
        """超时路径不应释放未持有的锁。"""
        raise AssertionError("未持有的订阅锁不应被释放")


def _configure_subscription_write(chain, repository) -> None:
    """为绕过构造器的搜索链注入显式同步修改作用域。"""
    chain.subscription_repository = repository

    @contextmanager
    def mutation_scope():
        """把同步修改命令委托给状态测试 repository。"""
        def update(subscribe_id, payload, _actor, existing=None, scene="update"):
            """执行测试更新并返回生产命令形状。"""
            updated = repository.update(subscribe_id, SubscriptionPatch(payload))
            return SubscriptionMutation(
                snapshot=updated,
                old=existing.to_dict() if existing else {},
                new=updated.to_dict() if updated else {},
            )

        yield SimpleNamespace(update=update)

    chain.sync_subscription_mutation_scope = mutation_scope


def _new_subscribe(created_at: datetime) -> SubscriptionSnapshot:
    """
    构造一个新建电影订阅。
    """
    return SubscriptionSnapshot(
        id=31,
        name="测试电影",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source="themoviedb",
        media_id="12345",
        season=None,
        custom_words=None,
        date=created_at.strftime("%Y-%m-%d %H:%M:%S"),
        state="N",
        episode_group=None,
    )


def test_new_subscribe_search_keeps_state_when_recently_created(monkeypatch) -> None:
    """
    新增 60 秒保护期内跳过搜索时，应保留 N 状态等待下一轮新增订阅搜索。
    """
    _SubscribeOper.subscribe = _new_subscribe(datetime.now())
    _SubscribeOper.updates = []
    media_chain_class = Mock()
    with patch.object(subscribe_search, "MediaChain", media_chain_class):
        chain = object.__new__(SubscribeChain)
        chain.subscription_repository = _SubscribeOper()
        chain.search(state="N", manual=False)

    media_chain_class.assert_not_called()
    assert _SubscribeOper.updates == []


def test_new_subscribe_search_marks_state_after_attempt(monkeypatch) -> None:
    """
    新增订阅越过保护期并实际尝试搜索后，应从 N 状态收敛为 R。
    """
    _SubscribeOper.subscribe = _new_subscribe(datetime.now() - timedelta(minutes=2))
    _SubscribeOper.updates = []
    media_chain = Mock()
    media_chain.recognize_media.return_value = None
    with patch.object(subscribe_search, "MediaChain", return_value=media_chain):
        chain = object.__new__(SubscribeChain)
        _configure_subscription_write(chain, _SubscribeOper())
        chain.search(state="N", manual=False)

    media_chain.recognize_media.assert_called_once()
    assert len(_SubscribeOper.updates) == 1
    subscribe_id, subscription_patch = _SubscribeOper.updates[0]
    assert subscribe_id == 31
    assert subscription_patch == SubscriptionPatch({"state": "R"})


def test_targeted_batch_searches_all_ids_without_state_scan(monkeypatch) -> None:
    """用户归属订阅批次只按指定 ID 顺序读取，不扩大为全局状态搜索。"""
    first = _new_subscribe(datetime.now() - timedelta(minutes=2))
    first = replace(first, state="R")
    second = replace(
        _new_subscribe(datetime.now() - timedelta(minutes=2)),
        id=32,
        name="测试电影 2",
        state="R",
    )
    subscribes = {first.id: first, second.id: second}
    subscribe_oper = Mock()
    subscribe_oper.get.side_effect = subscribes.get
    media_chain = Mock()
    media_chain.recognize_media.return_value = None

    with patch.object(subscribe_search, "MediaChain", return_value=media_chain):
        chain = object.__new__(SubscribeChain)
        chain.subscription_repository = subscribe_oper
        chain.search(sids=(31, 32), state=None, manual=False)

    assert [item.args for item in subscribe_oper.get.call_args_list] == [
        (31,),
        (32,),
        (31,),
        (32,),
    ]
    subscribe_oper.list.assert_not_called()
    assert media_chain.recognize_media.call_count == 2


def test_subscribe_search_aborts_when_lock_times_out(monkeypatch) -> None:
    """订阅搜索锁超时后必须中止，不能在无锁状态下继续访问订阅。"""
    monkeypatch.setattr(SubscribeChain, "_search_queue_lock", _TimedOutLock())
    subscribe_oper = Mock()
    progress = Mock()

    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = subscribe_oper
    chain.search(state="N", progress_callback=progress)

    subscribe_oper.assert_not_called()
    progress.assert_called_once_with(
        value=100,
        text="订阅搜索锁等待超时，已跳过本轮",
    )


def test_subscribe_search_releases_lock_when_repository_query_fails(monkeypatch) -> None:
    """取得搜索锁后即使订阅查询失败，也必须释放进程级互斥锁。"""
    lock = Mock()
    lock.acquire.return_value = True
    monkeypatch.setattr(SubscribeChain, "_search_queue_lock", lock)
    repository = Mock()
    repository.list.side_effect = RuntimeError("query failed")
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = repository

    with pytest.raises(RuntimeError, match="query failed"):
        chain.search(state="N")

    lock.release.assert_called_once_with()


def test_subscribe_search_uses_refreshed_state_for_final_reset(monkeypatch) -> None:
    """下载后重新读取的 R 状态不能再被旧 N 快照重置。"""
    subscribe = _new_subscribe(datetime.now() - timedelta(minutes=2))
    refreshed = replace(subscribe, state="R")
    _SubscribeOper.subscribe = subscribe
    _SubscribeOper.updates = []
    chain = object.__new__(SubscribeChain)
    _configure_subscription_write(chain, _SubscribeOper())
    process = Mock(return_value=refreshed)
    monkeypatch.setattr(chain, "_process_search_subscription", process)

    chain.search(state="N")

    process.assert_called_once()
    assert _SubscribeOper.updates == []


def test_subscribe_search_progress_preserves_public_callback_payload() -> None:
    """拆包后仍保持搜索开始和完成进度的既有文案及字段。"""
    subscribe = _new_subscribe(datetime.now() - timedelta(minutes=2))
    progress = Mock()

    SubscribeChain._report_search_progress(progress, subscribe, 1, 2)
    SubscribeChain._report_search_progress(progress, subscribe, 1, 2, finished=True)

    assert all(not item.args for item in progress.call_args_list)
    assert [item.kwargs for item in progress.call_args_list] == [
        {
            "value": 0,
            "text": "正在搜索订阅（1/2）测试电影 ...",
            "data": {"total": 2, "finished": 0, "current": 31},
        },
        {
            "value": 50,
            "text": "订阅搜索（1/2）处理完成",
            "data": {"total": 2, "finished": 1},
        },
    ]


def test_inline_search_conflict_does_not_report_false_completion(monkeypatch) -> None:
    """兼容搜索遇到同订阅冲突时必须报告未执行，而不是成功完成。"""
    subscribe = replace(
        _new_subscribe(datetime.now() - timedelta(minutes=2)),
        state="R",
    )
    repository = Mock()
    repository.get.return_value = subscribe
    progress = Mock()
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = repository
    chain._search_queue_lock = threading.Lock()
    chain._match_lock = threading.Lock()
    chain._subscription_execution_admission = SubscriptionExecutionAdmission()
    match_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="match",
        ttl_seconds=60,
    )
    assert match_lease is not None

    with patch.object(subscribe_search, "SearchChain", return_value=Mock()):
        chain.search(
            sid=subscribe.id,
            state=None,
            progress_callback=progress,
        )

    assert progress.call_args.kwargs == {
        "value": 100,
        "text": "订阅搜索结束，部分订阅本轮未执行或未完成",
        "data": {"total": 1, "finished": 0},
    }
    assert chain._subscription_execution_admission.release(match_lease) is True


def test_subscribe_match_aborts_when_lock_times_out(monkeypatch) -> None:
    """订阅匹配锁超时后必须中止，不能绕过防重复下载边界。"""
    monkeypatch.setattr(SubscribeChain, "_match_lock", _TimedOutLock())
    progress = Mock()

    chain = object.__new__(SubscribeChain)
    chain.match({"example.org": []}, progress_callback=progress)

    progress.assert_any_call(value=100, text="订阅匹配锁等待超时，已跳过本轮")
