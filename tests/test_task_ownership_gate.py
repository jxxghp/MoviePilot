"""TaskRegistry owner 静态门禁回归测试。"""

import textwrap
from pathlib import Path

from scripts.architecture.task_ownership import collect_task_owner_violations


def _scan_source(tmp_path: Path, source: str):
    """构造最小宿主源码并通过公开扫描入口返回违规。"""
    source_path = tmp_path / "app/api/sample.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(textwrap.dedent(source), encoding="utf-8")
    return collect_task_owner_violations(tmp_path)


def test_host_task_registry_calls_use_literal_owner() -> None:
    """当前 canonical 宿主的登记器调用必须保持 owner 零债务。"""
    assert collect_task_owner_violations() == []


def test_owner_gate_tracks_known_registry_without_matching_same_named_methods(
    tmp_path: Path,
) -> None:
    """门禁只检查可证明的登记器接收者，并区分缺失、动态和空 owner。"""
    violations = _scan_source(
        tmp_path,
        """
        from app.api.context import resolve_background_task_registry as resolve_registry
        from app.runtime.tasks import TaskRegistry, get_task_registry

        def schedule(task_registry: TaskRegistry, unrelated, dynamic_owner):
            unrelated.create(work())
            task_registry.create(work())
            resolve_registry(task_registry).create_sync(work, owner=dynamic_owner)
            get_task_registry().register(task, owner="   ")
            task_registry.submit_threadsafe(work(), loop=loop)
        """,
    )

    assert [violation.method for violation in violations] == [
        "create",
        "create_sync",
        "register",
        "submit_threadsafe",
    ]
    assert [violation.reason for violation in violations] == [
        "缺少显式 owner",
        "的 owner 必须是非空字符串字面量",
        "的 owner 必须是非空字符串字面量",
        "缺少显式 owner",
    ]


def test_owner_gate_accepts_aliases_and_stable_literal_owners(tmp_path: Path) -> None:
    """类、模块和工厂别名仍应被识别，稳定字符串 owner 可以通过。"""
    violations = _scan_source(
        tmp_path,
        """
        import app.runtime.tasks as runtime_tasks
        from app.runtime.tasks import TaskRegistry as Registry

        def schedule(task_registry: Registry):
            local_registry = runtime_tasks.TaskRegistry()
            task_registry.create(work(), owner="api.example.async")
            local_registry.create_sync(work, owner="api.example.sync")
            runtime_tasks.get_task_registry().register(
                task,
                owner="api.example.existing",
            )
            task_registry.submit_threadsafe(
                work(),
                loop=loop,
                owner="api.example.threadsafe",
            )
        """,
    )

    assert violations == []
