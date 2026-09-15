"""应用层整理模块的职责边界门禁。"""

import ast
import importlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
TRANSFER_ROOT = PROJECT_ROOT / "app" / "application" / "transfer"
WORKFLOW_PATH = TRANSFER_ROOT / "workflow.py"
JOBS_PATH = TRANSFER_ROOT / "jobs.py"


def test_job_manager_has_one_canonical_owner_and_legacy_exports() -> None:
    """JobManager 与作业锁只能在 jobs.py 定义，workflow.py 只保留兼容导出。"""
    workflow_tree = ast.parse(
        WORKFLOW_PATH.read_text(encoding="utf-8-sig"),
        filename=str(WORKFLOW_PATH),
    )
    jobs_tree = ast.parse(
        JOBS_PATH.read_text(encoding="utf-8-sig"),
        filename=str(JOBS_PATH),
    )

    workflow_classes = {
        node.name
        for node in workflow_tree.body
        if isinstance(node, ast.ClassDef)
    }
    jobs_classes = {
        node.name
        for node in jobs_tree.body
        if isinstance(node, ast.ClassDef)
    }
    workflow_assignments = {
        target.id
        for node in workflow_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    jobs_assignments = {
        target.id
        for node in jobs_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "JobManager" in jobs_classes
    assert "JobManager" not in workflow_classes
    assert "job_lock" in jobs_assignments
    assert "job_lock" not in workflow_assignments

    workflow = importlib.import_module("app.application.transfer.workflow")
    jobs = importlib.import_module("app.application.transfer.jobs")
    assert workflow.JobManager is jobs.JobManager
    assert workflow.job_lock is jobs.job_lock
    assert workflow.configure_directory_size is jobs.configure_directory_size
