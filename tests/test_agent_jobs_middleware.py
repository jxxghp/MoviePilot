import tempfile
import unittest
from pathlib import Path

from anyio import Path as AsyncPath

from app.agent.middleware.jobs import _alist_jobs, filter_active_jobs


class JobsMiddlewareTest(unittest.TestCase):
    def test_filter_active_jobs_only_keeps_pending_and_in_progress(self):
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

        self.assertEqual(["pending-job", "running-job"], active_job_ids)


class JobsMiddlewareAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_alist_jobs_sorts_job_directories_by_name(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            for job_id in ("z-job", "a-job", "m-job"):
                job_dir = root / job_id
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

            jobs = await _alist_jobs(AsyncPath(str(root)))

        self.assertEqual(["a-job", "m-job", "z-job"], [job["id"] for job in jobs])


if __name__ == "__main__":
    unittest.main()
