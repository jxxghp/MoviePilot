"""订阅执行治理最终受控规模门禁。"""

from app.application.subscription.candidates import CandidateIndex
from app.chain.subscribe.facade import SubscribeChain
from scripts.validation.subscription_governance_scale import (
    ScaleCase,
    _run_match_execution_case,
    run_acceptance,
)


def test_subscription_governance_controlled_scale_matrix() -> None:
    """两档最终矩阵必须同时满足正确性、压力、恢复和订阅准入门禁。"""
    result = run_acceptance()

    assert result["schema_version"] == 4
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
        for case in result["match_execution_cases"]
    )
    assert [
        case["site_peak_inflight_per_site"]
        for case in result["durable_cases"]
    ] == [1, 1]


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
