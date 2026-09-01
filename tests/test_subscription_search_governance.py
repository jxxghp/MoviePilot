"""订阅搜索队列接入、锁隔离和批次失败治理测试。"""

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.schemas.types import MediaType


class _ForbiddenLock:
    """正式队列路径若仍访问 Match 全局锁则立即失败。"""

    def acquire(self, **_kwargs):
        """禁止搜索队列取得历史长锁。"""
        raise AssertionError("持久搜索队列不应取得 Match 全局锁")

    def release(self):
        """禁止搜索队列释放从未取得的历史长锁。"""
        raise AssertionError("持久搜索队列不应释放 Match 全局锁")


class _SubscriptionRepository:
    """为搜索队列返回稳定的多订阅快照。"""

    def __init__(self, subscribes: list[SubscriptionSnapshot]) -> None:
        self._subscribes = {subscribe.id: subscribe for subscribe in subscribes}

    def list(self, _state: str = None) -> list[SubscriptionSnapshot]:
        """返回全部测试订阅。"""
        return list(self._subscribes.values())

    def get(self, subscribe_id: int) -> SubscriptionSnapshot | None:
        """按 ID 返回任务执行时重新读取的订阅。"""
        return self._subscribes.get(subscribe_id)


def _subscribe(subscribe_id: int) -> SubscriptionSnapshot:
    """构造越过新增保护期的活动电影订阅。"""
    return SubscriptionSnapshot(
        id=subscribe_id,
        name=f"治理电影 {subscribe_id}",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source="themoviedb",
        media_id=str(1000 + subscribe_id),
        state="R",
        date=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _chain(tmp_path, subscribes: list[SubscriptionSnapshot]):
    """构造注入持久队列且不初始化其他 Chain 依赖的搜索实例。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'search-governance.db'}")
    Base.metadata.create_all(engine)
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = _SubscriptionRepository(subscribes)
    chain.subscription_search_repository = TransactionalSubscriptionSearchRepository(
        sessionmaker(bind=engine)
    )
    chain.get_states_for_search = lambda state: state
    chain._rlock = _ForbiddenLock()
    return chain


def test_fallback_queue_executes_without_match_global_lock(tmp_path, monkeypatch):
    """R/P 兜底搜索在持久队列中执行，不受日常 Match 长锁阻塞。"""
    subscribes = [_subscribe(1), _subscribe(2)]
    chain = _chain(tmp_path, subscribes)
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    processed = []
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda subscribe, _searchchain: processed.append(subscribe.id) or subscribe,
    )

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert processed == [1, 2]
    assert batch.state == "completed"
    assert batch.finished_count == 2
    assert batch.failed_count == 0


def test_fallback_queue_continues_after_one_subscription_failure(tmp_path, monkeypatch):
    """单订阅异常不得中止批次后续任务，聚合终态必须暴露失败。"""
    subscribes = [_subscribe(3), _subscribe(4)]
    chain = _chain(tmp_path, subscribes)
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    processed = []

    def process(subscribe, _searchchain):
        """让首条失败并保持第二条正常完成。"""
        processed.append(subscribe.id)
        if subscribe.id == 3:
            raise RuntimeError("provider timeout")
        return replace(subscribe)

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert processed == [3, 4]
    assert batch.state == "failed"
    assert batch.finished_count == 1
    assert batch.failed_count == 1
    assert batch.last_error == "provider timeout"


def test_late_cancel_completes_when_download_submission_already_started(tmp_path, monkeypatch):
    """取消晚于下载器副作用边界时按真实结果完成，不能伪装成未执行取消。"""
    subscribe = _subscribe(5)
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    chain.subscription_download_repository = SimpleNamespace(
        has_started_for_task=lambda _task_id: False,
    )
    queue = chain.subscription_search_repository
    cancel_checks = iter((False, True))
    monkeypatch.setattr(queue, "is_cancel_requested", lambda _task_id: next(cancel_checks))
    def process(item, _searchchain):
        """模拟当前任务复用或完成下载后才收到取消。"""
        chain._mark_subscription_download_started()
        return item

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "completed"
    assert batch.finished_count == 1
    assert batch.cancelled_count == 0
