"""mypy 错误数只降不增 ratchet 的解析与对比逻辑测试。"""

from scripts.architecture.mypy_ratchet import compare_counts, parse_errors


MYPY_SAMPLE = """
app/application/a.py:12: error: Function is missing a type annotation [no-untyped-def]
app/application/a.py:15: error: Function is missing a type annotation [no-untyped-def]
     def run(self):
     ^
app/application/a.py:20: error: Missing type parameters for generic type "dict"  [type-arg]
Found 3 errors in 1 file (checked 500 source files)
"""


def test_parse_errors_aggregates_per_file_and_code() -> None:
    """错误行按文件与错误码聚合，源码上下文与摘要行不计入。"""
    report = parse_errors(MYPY_SAMPLE)

    assert report == {
        "app/application/a.py": {
            "no-untyped-def": 2,
            "type-arg": 1,
        }
    }


def test_parse_errors_buckets_missing_code_as_unknown() -> None:
    """缺少错误码的错误行归入 unknown 桶，不丢失计数。"""
    report = parse_errors("app/b.py:3: error: Something went wrong\n")

    assert report == {"app/b.py": {"unknown": 1}}


def test_ratchet_allows_removal_and_reduction() -> None:
    """文件删除或错误数减少必须允许通过。"""
    baseline = {"app/a.py": {"arg-type": 5, "misc": 2}}
    current = {"app/a.py": {"arg-type": 3}}

    assert compare_counts(baseline, current) == []


def test_ratchet_rejects_growth_and_new_errors() -> None:
    """既有错误码增长和新增文件/错误码必须同时给出精确诊断。"""
    baseline = {
        "app/a.py": {"arg-type": 5},
        "app/c.py": {"misc": 1},
    }
    current = {
        "app/a.py": {"arg-type": 6, "type-arg": 2},
        "app/b.py": {"no-untyped-def": 10},
        "app/c.py": {},
    }

    problems = compare_counts(baseline, current)

    assert any("app/a.py" in p and "arg-type" in p and "既有错误增长" in p for p in problems)
    assert any("app/a.py" in p and "type-arg" in p and "新增类型错误" in p for p in problems)
    assert any("app/b.py" in p and "新增类型错误" in p for p in problems)
