"""复杂度只降不增 ratchet 测试。"""

from scripts.architecture.complexity import compare_complexity


def test_complexity_ratchet_allows_removal_and_reduction() -> None:
    """既有超限入口被删除或缩短必须允许合并。"""
    baseline = {
        "api_endpoint": {"app/api/endpoints/a.py:large": 100},
        "application_public": {"app/application/a.py:Service.run": 180},
        "chain_public": {},
    }
    current = {
        "api_endpoint": {"app/api/endpoints/a.py:large": 90},
        "application_public": {},
        "chain_public": {},
    }

    assert compare_complexity(baseline, current) == []


def test_complexity_ratchet_rejects_growth_and_new_oversize() -> None:
    """既有入口增长和新增超预算入口必须同时给出精确诊断。"""
    baseline = {
        "api_endpoint": {"app/api/endpoints/a.py:large": 100},
        "application_public": {},
        "chain_public": {},
    }
    current = {
        "api_endpoint": {
            "app/api/endpoints/a.py:large": 101,
            "app/api/endpoints/b.py:new_endpoint": 81,
        },
        "application_public": {},
        "chain_public": {},
    }

    problems = compare_complexity(baseline, current)

    assert any("既有超限增长" in problem for problem in problems)
    assert any("新增超限" in problem for problem in problems)
