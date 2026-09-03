"""订阅兜底搜索的站点并发、冷却、恢复与取消测试。"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.site.observation import (
    SiteSearchObservation,
    report_site_search_outcome,
)
from app.application.subscription.sitebudget import (
    SiteBudgetClaim,
    SubscriptionSearchCancelled,
    SubscriptionSiteBudget,
    SubscriptionSiteBudgetMetrics,
    SubscriptionSiteBudgetUnavailable,
)
from app.chain.search.facade import SearchChain
from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.db.models.subscriptionsearch import SubscriptionSiteBudget as SiteBudgetRecord
from app.modules.indexer import IndexerModule, _classify_search_failure
from app.runtime.stop import ProcessStopState


def _repository(tmp_path):
    """构造使用独立 SQLite 文件的站点预算仓储。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'site-budget.db'}")
    Base.metadata.create_all(engine)
    return TransactionalSubscriptionSearchRepository(sessionmaker(bind=engine)), engine


def test_site_budget_allows_one_inflight_per_site_and_independent_sites(tmp_path):
    """同站点第二个调用必须等待，不同站点可立即并行。"""
    repository, _engine = _repository(tmp_path)

    first = repository.claim_site(site_id=1, owner="worker-a", lease_seconds=900)
    same_site = repository.claim_site(site_id=1, owner="worker-b", lease_seconds=900)
    other_site = repository.claim_site(site_id=2, owner="worker-b", lease_seconds=900)

    assert first.acquired is True
    assert same_site.acquired is False
    assert same_site.lease_token is None
    assert other_site.acquired is True


def test_site_budget_recovers_expired_inflight_lease(tmp_path):
    """进程遗留的过期站点租约可被新 worker 以新 token 恢复。"""
    repository, engine = _repository(tmp_path)
    first = repository.claim_site(site_id=20, owner="worker-a", lease_seconds=900)
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with Session(engine) as session:
        session.execute(
            SiteBudgetRecord.__table__.update()
            .where(SiteBudgetRecord.site_id == 20)
            .values(lease_expires_at=expired_at)
        )
        session.commit()

    recovered = repository.claim_site(site_id=20, owner="worker-b", lease_seconds=900)

    assert recovered.acquired is True
    assert recovered.lease_token != first.lease_token


def test_site_budget_applies_error_cooldown_and_gradual_success_recovery(tmp_path):
    """失败增加冷却计数，后续成功每次只恢复一级而非直接清零。"""
    repository, engine = _repository(tmp_path)
    claim = repository.claim_site(site_id=3, owner="worker-a", lease_seconds=900)
    now = datetime.now(timezone.utc)

    assert repository.finish_site(
        site_id=3,
        lease_token=claim.lease_token,
        outcome="timeout",
        next_allowed_at=(now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        error="request timeout",
    ) is True
    cooled = repository.claim_site(site_id=3, owner="worker-b", lease_seconds=900)
    assert cooled.acquired is False
    with Session(engine) as session:
        failed = session.execute(
            select(SiteBudgetRecord).where(SiteBudgetRecord.site_id == 3)
        ).scalar_one()
        assert failed.consecutive_failures == 1
        assert failed.success_streak == 0
        failed.next_allowed_at = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
        session.commit()

    recovered = repository.claim_site(site_id=3, owner="worker-b", lease_seconds=900)
    assert recovered.acquired is True
    assert recovered.consecutive_failures == 1
    assert repository.finish_site(
        site_id=3,
        lease_token=recovered.lease_token,
        outcome="success",
        next_allowed_at=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
    ) is True
    with Session(engine) as session:
        healthy = session.execute(
            select(SiteBudgetRecord).where(SiteBudgetRecord.site_id == 3)
        ).scalar_one()
        assert healthy.consecutive_failures == 0
        assert healthy.success_streak == 1
        assert healthy.last_outcome == "success"


def test_site_budget_ignores_legacy_success_interval(tmp_path):
    """升级前留下的正常成功间隔不能阻止新语义下的到期订阅。"""
    repository, engine = _repository(tmp_path)
    first = repository.claim_site(site_id=4, owner="worker-a", lease_seconds=900)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
    assert repository.finish_site(
        site_id=4,
        lease_token=first.lease_token,
        outcome="success",
        next_allowed_at=future,
    ) is True

    recovered = repository.claim_site(site_id=4, owner="worker-b", lease_seconds=900)

    assert recovered.acquired is True
    assert recovered.lease_token is not None
    with Session(engine) as session:
        record = session.execute(
            select(SiteBudgetRecord).where(SiteBudgetRecord.site_id == 4)
        ).scalar_one()
        assert record.next_allowed_at < future


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (RuntimeError("HTTP 429"), "rate_limited"),
        (RuntimeError("403 forbidden"), "forbidden"),
        (RuntimeError("Cookie 登录失效"), "login_invalid"),
        (TimeoutError("request timed out"), "timeout"),
    ],
)
def test_indexer_failures_map_to_site_cooldown_categories(error, outcome):
    """外站典型失败必须进入对应的站点级冷却类别。"""
    assert _classify_search_failure(error) == outcome


class _WaitingRepository:
    """始终返回未来重试时间的可取消预算仓储。"""

    def claim_site(self, *, site_id: int, owner: str, lease_seconds: int) -> SiteBudgetClaim:
        """返回未认领的未来预算。"""
        del owner, lease_seconds
        return SiteBudgetClaim(
            site_id=site_id,
            acquired=False,
            retry_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds"),
            consecutive_failures=0,
        )

    def finish_site(self, **_kwargs) -> bool:
        """等待测试不会取得租约，因此不应调用收口。"""
        raise AssertionError("未取得站点预算时不应收口")


def test_unavailable_site_budget_does_not_sleep_in_worker():
    """错误冷却中的站点应立即留待下轮，不占用同步 worker 等待。"""
    budget = SubscriptionSiteBudget(
        repository=_WaitingRepository(),
        owner="worker-a",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
    )

    with pytest.raises(SubscriptionSiteBudgetUnavailable):
        budget.acquire(9)


def test_site_budget_checks_cancellation_before_repository_claim():
    """取消或停机必须在创建任何站点租约前终止当前任务。"""
    class _ForbiddenRepository(_WaitingRepository):
        """取消路径不允许触发持久认领。"""

        def claim_site(self, **_kwargs) -> SiteBudgetClaim:
            """若取消检查失效则立即暴露。"""
            raise AssertionError("取消任务不应认领站点预算")

    budget = SubscriptionSiteBudget(
        repository=_ForbiddenRepository(),
        owner="worker-a",
        cancelled=lambda: True,
        stop_state=ProcessStopState(),
    )

    with pytest.raises(SubscriptionSearchCancelled):
        budget.acquire(9)


def test_site_budget_only_delays_external_failures():
    """正常完成立即恢复，外站错误仍按类别进入冷却。"""
    budget = SubscriptionSiteBudget(
        repository=_WaitingRepository(),
        owner="manual-worker",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
    )

    assert budget._next_delay("success", consecutive_failures=0) == 0.0  # pylint: disable=protected-access
    assert budget._next_delay("success", consecutive_failures=2) == 0.0  # pylint: disable=protected-access
    assert budget._next_delay("rate_limited", consecutive_failures=0) == 900.0  # pylint: disable=protected-access


def test_skipped_search_releases_budget_without_external_interval():
    """本地限流等未发请求结果不应伪造成功或追加外站间隔。"""
    captured = {}

    class _Repository(_WaitingRepository):
        """记录预算收口参数。"""

        def finish_site(self, **kwargs) -> bool:
            """保存收口事实供断言。"""
            captured.update(kwargs)
            return True

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    budget = SubscriptionSiteBudget(
        repository=_Repository(),
        owner="worker-a",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
        clock=lambda: now,
    )
    claim = SiteBudgetClaim(
        site_id=10,
        acquired=True,
        retry_at=now.isoformat(timespec="seconds"),
        consecutive_failures=0,
        lease_token="lease-token",
    )

    assert budget.finish(claim, SiteSearchObservation()) is True
    assert captured["outcome"] == "skipped"
    assert captured["next_allowed_at"] == now.isoformat(timespec="seconds")


def test_search_provider_reports_cooled_site_without_blocking_other_results():
    """错误冷却中的站点返回空页并记录聚合失败，而非阻塞 provider。"""
    repository = _WaitingRepository()
    metrics = SubscriptionSiteBudgetMetrics()
    budget = SubscriptionSiteBudget(
        repository=repository,
        owner="fallback-worker",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
        metrics=metrics,
    )
    chain = object.__new__(SearchChain)
    chain.configure_subscription_site_budget(budget)
    chain.search_site_torrents = lambda **_kwargs: ["unexpected"]

    result = chain._search_site_torrents_with_budget(  # pylint: disable=protected-access
        site={"id": 11, "name": "Cooling"},
        keyword="movie",
        mtype=None,
        page=0,
    )

    assert result == []
    failures = chain.consume_subscription_site_budget_failures()
    assert len(failures) == 1
    assert "站点 11" in failures[0]
    snapshot = metrics.snapshot()
    assert snapshot.request_count == 0
    assert snapshot.cooldown_skip_count == 1


def test_search_provider_releases_successful_site_budget():
    """真实站点页正常完成后必须释放租约并立即允许下一任务。"""
    captured = {}

    class _Repository(_WaitingRepository):
        """提供立即可用租约并记录收口参数。"""

        def claim_site(self, *, site_id: int, owner: str, lease_seconds: int) -> SiteBudgetClaim:
            """返回当前调用独占的站点租约。"""
            del owner, lease_seconds
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=True,
                retry_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                consecutive_failures=0,
                lease_token="lease-token",
            )

        def finish_site(self, **kwargs) -> bool:
            """记录成功收口事实。"""
            captured.update(kwargs)
            return True

    metrics = SubscriptionSiteBudgetMetrics()
    budget = SubscriptionSiteBudget(
        repository=_Repository(),
        owner="manual-worker",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
        metrics=metrics,
    )
    chain = object.__new__(SearchChain)
    chain.configure_subscription_site_budget(budget)

    def search_site_torrents(**_kwargs):
        """模拟索引器成功并发布观察结果。"""
        report_site_search_outcome(attempted=True, outcome="success")
        return ["torrent"]

    chain.search_site_torrents = search_site_torrents

    result = chain._search_site_torrents_with_budget(  # pylint: disable=protected-access
        site={"id": 12, "name": "Healthy"},
        keyword="movie",
        mtype=None,
        page=0,
    )

    assert result == ["torrent"]
    assert captured["site_id"] == 12
    assert captured["outcome"] == "success"
    assert captured["lease_token"] == "lease-token"
    snapshot = metrics.snapshot()
    assert snapshot.site_count == 1
    assert snapshot.request_count == 1
    assert snapshot.candidate_count == 1
    assert snapshot.failure_count == 0
    assert snapshot.release_failure_count == 0


def test_search_provider_aggregates_swallowed_indexer_failure(monkeypatch):
    """索引器吞掉外站异常返回空页时，provider 仍须暴露站点失败。"""
    captured = {}

    class _Repository(_WaitingRepository):
        """提供立即可用租约并记录失败收口。"""

        def claim_site(self, *, site_id: int, owner: str, lease_seconds: int) -> SiteBudgetClaim:
            """返回当前调用独占的站点租约。"""
            del owner, lease_seconds
            now = datetime.now(timezone.utc)
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=True,
                retry_at=now.isoformat(timespec="seconds"),
                consecutive_failures=0,
                lease_token="lease-token",
            )

        def finish_site(self, **kwargs) -> bool:
            """记录预算收口，确认错误冷却仍由 budget.finish 负责。"""
            captured.update(kwargs)
            return True

    def execute_search(_site, _request):
        """模拟 IndexerModule 捕获的 HTTP 429。"""
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(lambda _site, _keyword=None: True),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__execute_search",
        staticmethod(execute_search),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__indexer_statistic",
        staticmethod(lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__parse_result",
        staticmethod(lambda **_kwargs: []),
    )

    metrics = SubscriptionSiteBudgetMetrics()
    budget = SubscriptionSiteBudget(
        repository=_Repository(),
        owner="fallback-worker",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
        metrics=metrics,
    )
    chain = object.__new__(SearchChain)
    chain.configure_subscription_site_budget(budget)
    chain.search_site_torrents = object.__new__(IndexerModule).search_torrents

    result = chain._search_site_torrents_with_budget(  # pylint: disable=protected-access
        site={"id": 13, "name": "Flaky"},
        keyword="movie",
        mtype=None,
        page=0,
    )

    assert result == []
    assert captured["outcome"] == "rate_limited"
    assert captured["error"] == "HTTP 429"
    failures = chain.consume_subscription_site_budget_failures()
    assert len(failures) == 1
    assert "Flaky" in failures[0]
    assert "HTTP 429" in failures[0]
    snapshot = metrics.snapshot()
    assert snapshot.request_count == 1
    assert snapshot.failure_count == 1
    assert snapshot.cooldown_seconds == 900.0


def test_search_provider_logs_site_budget_release_failure(monkeypatch):
    """站点租约收口返回失败时必须进入摘要并即时记录错误。"""
    class _Repository(_WaitingRepository):
        """提供租约但拒绝以当前 token 收口。"""

        def claim_site(self, *, site_id: int, owner: str, lease_seconds: int) -> SiteBudgetClaim:
            """返回当前调用独占的站点租约。"""
            del owner, lease_seconds
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=True,
                retry_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                consecutive_failures=0,
                lease_token="stale-token",
            )

        def finish_site(self, **_kwargs) -> bool:
            """模拟 token 已失效的 CAS 收口。"""
            return False

    metrics = SubscriptionSiteBudgetMetrics()
    budget = SubscriptionSiteBudget(
        repository=_Repository(),
        owner="fallback-worker",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
        metrics=metrics,
    )
    chain = object.__new__(SearchChain)
    chain.configure_subscription_site_budget(budget)
    chain.search_site_torrents = lambda **_kwargs: ["torrent"]
    errors = []
    monkeypatch.setattr("app.chain.search.provider.logger.error", lambda message, **_kwargs: errors.append(message))

    result = chain._search_site_torrents_with_budget(  # pylint: disable=protected-access
        site={"id": 14, "name": "Stale"},
        keyword="movie",
        mtype=None,
        page=0,
    )

    assert result == ["torrent"]
    assert metrics.snapshot().release_failure_count == 1
    assert errors == ["订阅站点预算释放失败: site_id=14 site=Stale"]
