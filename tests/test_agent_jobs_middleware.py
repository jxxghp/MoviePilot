"""
验证任务中间件的活跃任务过滤、目录排序和损坏文本处理。
"""

import anyio
from anyio import Path as AsyncPath

from app.agent.middleware.jobs import _alist_jobs, filter_active_jobs


def test_filter_active_jobs_only_keeps_pending_and_in_progress():
    """
    筛选任务时只保留待执行和执行中的任务。
    """
    jobs_metadata = [
        {
            "id": "pending-job",
            "name": "待执行任务",
            "description": "desc",
            "path": "/tmp/pending/JOB.md",
            "schedule": "once",
            "status": "pending",
            "last_run": None,
        },
        {
            "id": "running-job",
            "name": "执行中任务",
            "description": "desc",
            "path": "/tmp/running/JOB.md",
            "schedule": "recurring",
            "status": "in_progress",
            "last_run": "2026-05-10 10:00",
        },
        {
            "id": "completed-recurring-job",
            "name": "已完成循环任务",
            "description": "desc",
            "path": "/tmp/completed/JOB.md",
            "schedule": "recurring",
            "status": "completed",
            "last_run": "2026-05-10 11:00",
        },
        {
            "id": "cancelled-job",
            "name": "已取消任务",
            "description": "desc",
            "path": "/tmp/cancelled/JOB.md",
            "schedule": "once",
            "status": "cancelled",
            "last_run": None,
        },
    ]

    active_job_ids = [job["id"] for job in filter_active_jobs(jobs_metadata)]

    assert active_job_ids == ["pending-job", "running-job"]


def test_alist_jobs_sorts_job_directories_by_name(tmp_path):
    """
    加载任务元数据时按任务目录名稳定排序。
    """
    for job_id in ("z-job", "a-job", "m-job"):
        job_dir = tmp_path / job_id
        job_dir.mkdir()
        (job_dir / "JOB.md").write_text(
            f"""---
name: {job_id}
description: test
schedule: once
status: pending
---
# {job_id}
""",
            encoding="utf-8",
        )

    jobs = anyio.run(_alist_jobs, AsyncPath(str(tmp_path)))

    assert [job["id"] for job in jobs] == ["a-job", "m-job", "z-job"]


def test_alist_jobs_tolerates_invalid_utf8_bytes(tmp_path):
    """
    加载任务元数据时非法 UTF-8 字节不应中断整个 Agent。
    """
    job_dir = tmp_path / "broken-job"
    job_dir.mkdir()
    (job_dir / "JOB.md").write_bytes(
        b"""---
name: broken-job
description: test
schedule: once
status: pending
---
# broken-job
\x90
"""
    )

    jobs = anyio.run(_alist_jobs, AsyncPath(str(tmp_path)))

    assert [job["id"] for job in jobs] == ["broken-job"]
