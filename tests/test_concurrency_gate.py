"""原生并发事实门禁回归测试。"""

import textwrap
from pathlib import Path

from scripts.architecture.concurrency import collect_concurrency, compare_concurrency


def _write_source(root: Path, relative: str, source: str) -> None:
    """写入临时宿主源码，供 AST collector 做结构性验证。"""
    source_path = root / relative
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_concurrency_gate_resolves_aliases_and_runtime_sources(tmp_path: Path) -> None:
    """canonical 别名、TaskGroup、事件循环和 Executor 来源必须完整枚举。"""
    _write_source(
        tmp_path,
        "app/doctor/sample.py",
        """
        import asyncio as aio
        import concurrent.futures as cf
        from asyncio import AbstractEventLoop, TaskGroup as TG, create_task as spawn
        from concurrent.futures import ThreadPoolExecutor as Pool
        from threading import Thread as WorkerThread

        async def run(job, typed_loop: AbstractEventLoop):
            loop = aio.get_running_loop()
            spawn(job())
            aio.create_task(job())
            loop.create_task(job())
            typed_loop.run_in_executor(None, job)
            async with TG() as group:
                group.create_task(job())
            with cf.ThreadPoolExecutor() as executor:
                executor.submit(job)
            with Pool() as executor:
                executor.map(job, [])
            WorkerThread(target=job)
        """,
    )

    facts = collect_concurrency(tmp_path)

    assert facts["app/doctor/sample.py:asyncio.create_task"]["owners"] == {"run": 2}
    assert facts["app/doctor/sample.py:asyncio.AbstractEventLoop.create_task"]["owners"] == {
        "run": 1
    }
    assert facts["app/doctor/sample.py:asyncio.AbstractEventLoop.run_in_executor"][
        "owners"
    ] == {"run": 1}
    assert facts["app/doctor/sample.py:asyncio.TaskGroup"]["owners"] == {"run": 1}
    assert facts["app/doctor/sample.py:asyncio.TaskGroup.create_task"]["owners"] == {
        "run": 1
    }
    assert facts["app/doctor/sample.py:concurrent.futures.ThreadPoolExecutor"][
        "owners"
    ] == {"run": 2}
    assert facts["app/doctor/sample.py:concurrent.futures.Executor.submit"]["owners"] == {
        "run": 1
    }
    assert facts["app/doctor/sample.py:concurrent.futures.Executor.map"]["owners"] == {
        "run": 1
    }
    assert facts["app/doctor/sample.py:threading.Thread"]["owners"] == {"run": 1}


def test_concurrency_gate_excludes_unproven_same_named_calls(tmp_path: Path) -> None:
    """普通同名函数和对象方法没有 canonical 来源时不得误报。"""
    _write_source(
        tmp_path,
        "app/testing/fakes.py",
        """
        def create_task():
            return None

        def run_in_threadpool():
            return None

        def run(obj):
            create_task()
            run_in_threadpool()
            obj.create_task()
            obj.submit()
        """,
    )

    assert collect_concurrency(tmp_path) == {}


def test_concurrency_facts_ignore_line_drift_and_preserve_counts(tmp_path: Path) -> None:
    """空行漂移不得改变事实键，同一行多个调用必须保留精确计数。"""
    relative = "app/runtime/sample.py"
    _write_source(
        tmp_path,
        relative,
        """
        import asyncio
        async def run(first, second):
            asyncio.create_task(first()); asyncio.create_task(second())
        """,
    )
    baseline = collect_concurrency(tmp_path)
    _write_source(
        tmp_path,
        relative,
        """


        import asyncio
        async def run(first, second):
            asyncio.create_task(first()); asyncio.create_task(second())
        """,
    )

    current = collect_concurrency(tmp_path)
    assert current == baseline
    assert current["app/runtime/sample.py:asyncio.create_task"]["owners"] == {"run": 2}


def test_concurrency_gate_scans_complete_app_and_keeps_exact_exclusions(
    tmp_path: Path,
) -> None:
    """默认扫描必须覆盖完整 app，同时排除 Plugin、SDK 和 Compat 表面。"""
    source = """
        import asyncio
        asyncio.create_task(job())
    """
    _write_source(tmp_path, "app/doctor/check.py", source)
    _write_source(tmp_path, "app/plugins/copy.py", source)
    _write_source(tmp_path, "app/sdk/facade.py", source)
    _write_source(tmp_path, "app/runtime/compat/legacy.py", source)

    facts = collect_concurrency(tmp_path)
    assert list(facts) == ["app/doctor/check.py:asyncio.create_task"]
    assert facts["app/doctor/check.py:asyncio.create_task"]["owners"] == {"<module>": 1}


def test_concurrency_ratchet_rejects_growth_and_stale_low_water() -> None:
    """新增 owner、调用增长和未刷新的下降事实都必须阻断 ratchet。"""
    baseline = {
        "app/runtime/tasks.py:asyncio.create_task": {
            "target": "asyncio.create_task",
            "owners": {"Runtime.run": 2, "Runtime.old": 1},
        },
    }
    current = {
        "app/runtime/tasks.py:asyncio.create_task": {
            "target": "asyncio.create_task",
            "owners": {"Runtime.run": 3, "Runtime.new": 1},
        },
        "app/api/system.py:threading.Thread": {
            "target": "threading.Thread",
            "owners": {"system": 1},
        },
    }

    problems = compare_concurrency(baseline, current)

    assert any("新增原生并发事实" in problem for problem in problems)
    assert any("Runtime.run 3>2" in problem for problem in problems)
    assert any("Runtime.new 1>0" in problem for problem in problems)
    assert any("Runtime.old 0<1" in problem for problem in problems)
