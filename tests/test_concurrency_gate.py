"""原生并发事实门禁回归测试。"""

import textwrap
from pathlib import Path

from scripts.architecture.concurrency import collect_concurrency, compare_concurrency


def test_concurrency_gate_tracks_nested_owners_and_known_primitives(tmp_path: Path) -> None:
    """嵌套函数、类方法和常见原生并发入口都必须被枚举。"""
    source_path = tmp_path / "app/scheduler/sample.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        textwrap.dedent(
            """
            import asyncio
            import threading
            from concurrent.futures import ThreadPoolExecutor

            class Runner:
                async def run(self):
                    asyncio.create_task(self.child())
                    threading.Timer(1, self.child).start()
                    with ThreadPoolExecutor() as executor:
                        executor.submit(self.child)

                async def child(self):
                    await asyncio.to_thread(lambda: None)
            """
        ),
        encoding="utf-8",
    )

    facts = collect_concurrency(tmp_path, scan_roots=("app/scheduler",))
    assert facts["app/scheduler/sample.py:8:create_task"]["owner"] == "Runner.run"
    assert facts["app/scheduler/sample.py:9:Timer"]["owner"] == "Runner.run"
    assert facts["app/scheduler/sample.py:10:ThreadPoolExecutor"]["owner"] == "Runner.run"
    assert facts["app/scheduler/sample.py:14:to_thread"]["owner"] == "Runner.child"


def test_concurrency_ratchet_rejects_new_facts_and_owner_drift() -> None:
    """新增并发调用和 owner 漂移必须阻断 ratchet。"""
    baseline = {
        "app/runtime/tasks.py:10:create_task": {
            "owner": "Runtime.run",
            "target": "create_task",
        },
    }
    current = {
        "app/runtime/tasks.py:10:create_task": {
            "owner": "Runtime.changed",
            "target": "create_task",
        },
        "app/api/system.py:20:Thread": {
            "owner": "system",
            "target": "Thread",
        },
    }

    problems = compare_concurrency(baseline, current)
    assert any("owner 漂移" in problem for problem in problems)
    assert any("新增原生并发事实" in problem for problem in problems)
