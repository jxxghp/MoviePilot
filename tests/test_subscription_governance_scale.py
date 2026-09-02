"""订阅执行治理最终受控规模门禁。"""

from scripts.validation.subscription_governance_scale import run_acceptance


def test_subscription_governance_controlled_scale_matrix() -> None:
    """两档最终矩阵必须同时满足正确性、压力、恢复和订阅准入门禁。"""
    result = run_acceptance()

    assert result["schema_version"] == 2
    assert result["passed"] is True
    assert all(result["gates"].values())
    assert result["gates"]["subscription_admission_serializes"] is True
    assert [case["subscription_count"] for case in result["match_cases"]] == [100, 200]
    assert [case["site_count"] for case in result["match_cases"]] == [10, 20]
    assert min(case["candidate_count"] for case in result["match_cases"]) >= 1000
