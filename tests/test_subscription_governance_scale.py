"""订阅执行治理最终受控规模门禁。"""

import threading

import pytest

from app.application.site.observation import capture_site_search_observation
from app.application.subscription.candidates import CandidateIndex
from app.application.subscription.sitebudget import SiteBudgetClaim
from app.chain.search.facade import SearchChain
from app.chain.subscribe.facade import SubscribeChain
from scripts.validation import subscription_governance_scale as scale
from scripts.validation.subscription_governance_scale import (
    ScaleCase,
    _run_durable_governance,
    _run_match_execution_case,
    run_acceptance,
)


def test_subscription_governance_controlled_scale_matrix() -> None:
    """两档最终矩阵必须同时满足正确性、压力、恢复和订阅准入门禁。"""
    result = run_acceptance()

    assert result["schema_version"] == 5
    assert result["passed"] is True
    assert all(result["gates"].values())
    assert result["method"]["match_entrypoint"] == "SubscribeChain.match"
    assert result["method"]["download_selection"] == "DownloadChain.batch_download"
    assert result["method"]["fixed_external_boundaries"] == [
        "TMDB",
        "media_server",
        "downloader",
    ]
    assert result["method"]["fixed_persistence_boundaries"] == [
        "subscription_repository",
        "download_facts",
        "subscription_progress",
        "completion_side_effects",
    ]
    assert result["method"]["fixed_policy_inputs"] == [
        "site_mapping",
        "system_configuration",
        "subscription_filters",
        "torrent_attribute_filter",
        "download_preparation",
    ]
    assert result["method"]["production_network_slo"] is False
    assert result["gates"]["subscription_admission_serializes"] is True
    assert result["gates"]["match_execution_candidate_sets_equal"] is True
    assert result["gates"]["match_execution_download_sets_equal"] is True
    assert result["gates"]["match_execution_missing_sets_equal"] is True
    assert result["gates"]["match_execution_completion_sets_equal"] is True
    assert result["gates"]["match_info_logs_bounded"] is True
    assert [case["subscription_count"] for case in result["match_cases"]] == [100, 200]
    assert [case["site_count"] for case in result["match_cases"]] == [10, 20]
    assert min(case["candidate_count"] for case in result["match_cases"]) >= 1000
    assert [
        case["candidate_check_reduction_percent"]
        for case in result["match_cases"]
    ] == [99.0, 99.0]
    assert [
        case["matched_candidates"]["actual_count"]
        for case in result["match_execution_cases"]
    ] == [1000, 2400]
    assert [
        case["downloaded_candidates"]["actual_count"]
        for case in result["match_execution_cases"]
    ] == [100, 200]
    assert all(
        case["downloaded_candidates"]["duplicate_count"] == 0
        and case["completed_subscriptions"]["duplicate_count"] == 0
        and case["info_log_count"] == 2
        and case["info_log_bounded"] is True
        for case in result["match_execution_cases"]
    )
    assert [
        case["site_peak_inflight_per_site"]
        for case in result["durable_cases"]
    ] == [1, 1]
    assert all(
        case["site_request_boundary_active"] == 0
        and case["site_request_boundary_peak"] == 1
        and case["site_pressure_owner_count"] == 2
        and case["site_pressure_concurrency_verified"]
        and case["site_pressure_success_release_reused"]
        and case["site_pressure_error_observation_cooled"]
        and case["site_pressure_valid"]
        for case in result["durable_cases"]
    )
    assert all(
        observation["request_active"] == 0
        and observation["request_peak"] == 1
        and observation["owners_finished"] is True
        and observation["request_active_at_rejection"] == 1
        and observation["budget_rejections"] == 1
        for case in result["durable_cases"]
        for observation in case["site_observations"]
    )


@pytest.mark.parametrize("mutation", ["bypass", "early_release", "allow_concurrent"])
def test_scale_validator_rejects_fake_site_pressure(monkeypatch, tmp_path, mutation):
    """绕过 wrapper、提前释放或并发放行时，请求边界峰值必须暴露门禁失效。"""
    if mutation == "bypass":
        def bypass_wrapper(self, *, site, keyword, mtype, page):
            """模拟绕过预算 wrapper 的请求路径。"""
            return self.search_site_torrents(
                site=site,
                keyword=keyword,
                mtype=mtype,
                page=page,
            )

        monkeypatch.setattr(
            SearchChain,
            "_search_site_torrents_with_budget",
            bypass_wrapper,
        )
    elif mutation == "early_release":
        release_guard = threading.Lock()

        def early_release_wrapper(self, *, site, keyword, mtype, page):
            """模拟请求边界前错误释放站点租约。"""
            budget = self._subscription_site_budget
            # 将错误释放本身串行化，避免第二个 owner 在首个释放动作完成前
            # 被正常拒绝，从而掩盖“请求期间已失去租约”的变异。
            with release_guard:
                claim = budget.acquire(site["id"])
                with capture_site_search_observation() as observation:
                    budget.finish(claim, observation)
            return self.search_site_torrents(
                site=site,
                keyword=keyword,
                mtype=mtype,
                page=page,
            )

        monkeypatch.setattr(
            SearchChain,
            "_search_site_torrents_with_budget",
            early_release_wrapper,
        )
    else:
        original_claim = scale.TransactionalSubscriptionSearchRepository.claim_site

        def allow_concurrent_claim(self, *, site_id, owner, lease_seconds):
            """模拟仓储错误地向第二 owner 发放同站点租约。"""
            claim = original_claim(
                self,
                site_id=site_id,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if claim.acquired:
                return claim
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=True,
                retry_at=claim.retry_at,
                consecutive_failures=claim.consecutive_failures,
                lease_token=f"mutant-{owner}",
            )

        monkeypatch.setattr(
            scale.TransactionalSubscriptionSearchRepository,
            "claim_site",
            allow_concurrent_claim,
        )

    result = _run_durable_governance(
        ScaleCase(f"mutant-{mutation}", 2, 1, 2, 1),
        tmp_path,
    )

    assert result["site_pressure_valid"] is False
    assert result["site_peak_inflight_per_site"] >= 2
    assert result["site_request_boundary_peak"] >= 2


def test_scale_validator_rejects_unfinished_site_wrapper(monkeypatch, tmp_path):
    """延后已登记但 wrapper 尚未返回时，压力门禁必须拒绝并暴露未收口 owner。"""
    original_wrapper = SearchChain._search_site_torrents_with_budget
    release_stalled = threading.Event()
    threads_before = set(threading.enumerate())

    def stall_after_rejection(self, *, site, keyword, mtype, page):
        """模拟预算拒绝已记录、调用方却未取得返回值的挂起路径。"""
        original_record_deferred = self.record_subscription_site_budget_deferred

        def record_deferred_and_stall(deferral) -> None:
            """在拒绝已登记后阻塞原始 wrapper 的返回。"""
            original_record_deferred(deferral)
            release_stalled.wait()

        self.record_subscription_site_budget_deferred = record_deferred_and_stall
        try:
            return original_wrapper(
                self,
                site=site,
                keyword=keyword,
                mtype=mtype,
                page=page,
            )
        finally:
            self.record_subscription_site_budget_deferred = original_record_deferred

    monkeypatch.setattr(
        SearchChain,
        "_search_site_torrents_with_budget",
        stall_after_rejection,
    )
    monkeypatch.setattr(scale, "_SITE_PRESSURE_SYNC_TIMEOUT", 0.05)

    try:
        result = _run_durable_governance(
            ScaleCase("mutant-unfinished", 2, 1, 2, 1),
            tmp_path,
        )

        assert result["site_pressure_valid"] is False
        assert result["site_pressure_concurrency_verified"] is False
        assert result["site_observations"][0]["owners_finished"] is False
    finally:
        release_stalled.set()
        for thread in set(threading.enumerate()) - threads_before:
            thread.join(timeout=1)


def test_scale_validator_rejects_candidate_loss(monkeypatch) -> None:
    """候选路由少返回任意资源时，完整匹配集合门禁必须失败。"""
    original_route = CandidateIndex.route_for_match

    def route_with_loss(self, *args, **kwargs):
        routed = original_route(self, *args, **kwargs)
        for domain, contexts in routed.items():
            if contexts:
                return {**routed, domain: contexts[1:]}
        return routed

    monkeypatch.setattr(CandidateIndex, "route_for_match", route_with_loss)

    result = _run_match_execution_case(ScaleCase("candidate-loss", 4, 2, 8, 4))

    assert result["matched_candidates"]["equal"] is False
    assert result["matched_candidates"]["missing_sample"]


def test_scale_validator_rejects_duplicate_download_and_completion(monkeypatch) -> None:
    """同订阅重复下载或错误完成时，多重集合门禁必须失败。"""
    from scripts.validation import subscription_governance_scale as scale

    original_download = scale._ScaleDownloadBoundary.download_single

    def duplicate_download(self, context, *, governance, **kwargs):
        result = original_download(
            self,
            context,
            governance=governance,
            **kwargs,
        )
        original_download(
            self,
            context,
            governance=governance,
            **kwargs,
        )
        return result

    def finish_every_subscription(
        self,
        *,
        subscribe,
        meta,
        mediainfo,
        **_kwargs,
    ):
        self._SubscribeChain__finish_subscribe(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
        )

    monkeypatch.setattr(
        scale._ScaleDownloadBoundary,
        "download_single",
        duplicate_download,
    )
    monkeypatch.setattr(
        SubscribeChain,
        "finish_subscribe_or_not",
        finish_every_subscription,
    )

    result = _run_match_execution_case(ScaleCase("duplicate-effects", 4, 2, 8, 4))

    assert result["downloaded_candidates"]["equal"] is False
    assert result["downloaded_candidates"]["duplicate_count"] == 4
    assert result["completed_subscriptions"]["equal"] is False
    assert result["completed_subscriptions"]["unexpected_sample"]
