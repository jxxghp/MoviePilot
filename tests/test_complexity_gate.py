"""复杂度只降不增 ratchet 测试。"""

from pathlib import Path

from scripts.architecture.complexity import (
    collect_complexity_v2,
    compare_complexity,
    compare_complexity_v2,
)


def _assignments(indent: str, count: int) -> str:
    """生成足够长的临时语句体，精确触发复杂度阈值。"""
    return "\n".join(f"{indent}value_{index} = {index}" for index in range(count))


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


def test_complexity_v2_walks_dunder_match_trystar_and_nested_class_owners(
    tmp_path: Path,
) -> None:
    """完整 AST 遍历必须覆盖 dunder、Match、TryStar 和函数内嵌套类。"""
    scheduler = tmp_path / "app/scheduler"
    scheduler.mkdir(parents=True)
    (scheduler / "dunder.py").write_text(
        "class Runner:\n"
        "    def __hidden(self):\n"
        f"{_assignments('        ', 151)}\n",
        encoding="utf-8",
    )
    (scheduler / "control_flow.py").write_text(
        "def dispatch(value):\n"
        "    match value:\n"
        "        case 1:\n"
        "            def matched():\n"
        f"{_assignments('                ', 151)}\n"
        "            return matched()\n\n"
        "def guarded():\n"
        "    try:\n"
        "        raise ExceptionGroup('failure', [ValueError()])\n"
        "    except* ValueError:\n"
        "        def recovered():\n"
        f"{_assignments('            ', 151)}\n"
        "        return recovered()\n",
        encoding="utf-8",
    )
    nested_classes = (
        "class Outer:\n"
        "    def first(self):\n"
        "        class Local:\n"
        f"{_assignments('            ', 501)}\n\n"
        "    def second(self):\n"
        "        class Local:\n"
        f"{_assignments('            ', 501)}\n"
    )
    (scheduler / "nested_classes.py").write_text(nested_classes, encoding="utf-8")
    (scheduler / "conditional.py").write_text(
        "if enabled:\n"
        "    class Conditional:\n"
        f"{_assignments('        ', 501)}\n",
        encoding="utf-8",
    )

    report = collect_complexity_v2(tmp_path)

    assert "app/scheduler/dunder.py:Runner.__hidden" in report["method"]
    assert "app/scheduler/control_flow.py:dispatch.matched" in report["method"]
    assert "app/scheduler/control_flow.py:guarded.recovered" in report["method"]
    assert "app/scheduler/conditional.py:Conditional" in report["class"]
    assert "app/scheduler/nested_classes.py:Outer.first.Local" in report["class"]
    assert "app/scheduler/nested_classes.py:Outer.second.Local" in report["class"]
