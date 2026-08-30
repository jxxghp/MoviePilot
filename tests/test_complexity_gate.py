"""复杂度只降不增 ratchet 测试。"""

from scripts.architecture.complexity import compare_complexity, compare_complexity_v2


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


def test_complexity_v2_ratchet_checks_categories_outside_public_rules() -> None:
    """v2 基线必须实际比较私有方法、类和文件类别。"""
    baseline = {"method": {"app/scheduler/a.py:Runner.run": 160}, "class": {}, "file": {}}
    current = {
        "method": {"app/scheduler/a.py:Runner.run": 161},
        "class": {"app/scheduler/a.py:Runner": 600},
        "file": {"app/scheduler/a.py": 1200},
    }

    problems = compare_complexity_v2(baseline, current)
    assert any("method: 既有超限增长" in problem for problem in problems)
    assert any("class: 新增超限" in problem for problem in problems)
    assert any("file: 新增超限" in problem for problem in problems)
