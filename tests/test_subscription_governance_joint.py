"""日常 Match、手工/兜底队列和站点预算的联合治理测试。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.application.site.observation import report_site_search_outcome
from app.application.subscription.sitebudget import SiteBudgetClaim, SubscriptionSiteBudget
from app.chain.search.facade import SearchChain
from app.chain.search.provider import SearchProviderOwner
from app.runtime.stop import ProcessStopState


class _Progress:
    """收集 provider 进度而不依赖全局进度缓存。"""

    def __init__(self) -> None:
        self.values: list[float] = []

    def update(self, *, value: float, **_kwargs) -> None:
        """记录一次进度值。"""
        self.values.append(value)


class _MixedBudgetRepository:
    """模拟一个冷却站点和一个立即可用站点。"""

    def __init__(self) -> None:
        self.finished_sites: list[int] = []

    def claim_site(self, *, site_id: int, owner: str, lease_seconds: int) -> SiteBudgetClaim:
        """站点 1 保持冷却，站点 2 返回独占租约。"""
        del owner, lease_seconds
        now = datetime.now(timezone.utc)
        if site_id == 1:
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=False,
                retry_at=(now + timedelta(minutes=10)).isoformat(timespec="seconds"),
                consecutive_failures=1,
            )
        return SiteBudgetClaim(
            site_id=site_id,
            acquired=True,
            retry_at=now.isoformat(timespec="seconds"),
            consecutive_failures=0,
            lease_token=f"lease-{site_id}",
        )

    def finish_site(self, *, site_id: int, **_kwargs) -> bool:
        """记录成功执行并释放的站点。"""
        self.finished_sites.append(site_id)
        return True


def test_cooled_site_does_not_block_independent_site_or_mark_batch_failed():
    """冷却站点延后后其它站点仍完成，调用方只收到可重新入队的事实。"""
    repository = _MixedBudgetRepository()
    budget = SubscriptionSiteBudget(
        repository=repository,
        owner="fallback-task",
        cancelled=lambda: False,
        stop_state=ProcessStopState(),
    )
    chain = object.__new__(SearchChain)
    chain._runtime_config = SimpleNamespace(search_threadpool_size=2)
    chain.configure_subscription_site_budget(budget)
    chain._should_continue_search_pages = lambda **_kwargs: False

    def search_site_torrents(*, site, **_kwargs):
        """模拟可用站点返回一条结果并发布成功事实。"""
        report_site_search_outcome(attempted=True, outcome="success")
        return [site["id"]]

    chain.search_site_torrents = search_site_torrents
    results = []
    progress = _Progress()

    SearchProviderOwner._collect_sync_site_results(  # pylint: disable=protected-access
        chain,
        keyword="movie",
        indexer_sites=[
            {"id": 1, "name": "Cooling"},
            {"id": 2, "name": "Healthy"},
        ],
        search_pages=[0],
        search_keyword="movie",
        media_type=None,
        results=results,
        progress=progress,
    )

    assert results == [2]
    assert repository.finished_sites == [2]
    deferrals = chain.consume_subscription_site_budget_deferrals()
    assert len(deferrals) == 1
    assert deferrals[0].site_id == 1
    failures = chain.consume_subscription_site_budget_failures()
    assert failures == ()
    assert progress.values[-1] == 100
