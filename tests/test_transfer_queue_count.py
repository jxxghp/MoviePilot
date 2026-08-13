"""
整理队列批次计数口径测试。

背景:作业视图中的任务完成后仅标记终态,作业要等关联任务全部终态才整体移除,
因此追更/分批场景下已完成任务会跨批次残留。批次开始日志与进度分母若用全量
total(),会把历史任务计入「当前共 N 个文件」(如实际只处理 2 个却显示 8 个),
且进度百分比永远走不满。批次统计必须只数未终态任务。
"""
import unittest

from app.chain.transfer import JobManager

from tests.test_transfer_job_manager import make_task


class TransferQueueCountTest(unittest.TestCase):

    @staticmethod
    def _build_jobview_with_stale_tasks() -> JobManager:
        """
        构造用户实测的 8→2 场景:同一作业 8 个任务,6 个已完成(作业因
        仍有未终态任务不会被移除),2 个等待处理。
        """
        jobview = JobManager()
        completed = [make_task(episode) for episode in range(1, 7)]
        waiting = [make_task(episode) for episode in range(7, 9)]
        for task in completed + waiting:
            assert jobview.add_task(task)
        for task in completed:
            jobview.finish_task(task)
            # 作业尚有未终态任务,不会被移除,已完成任务随作业残留
            jobview.try_remove_job(task)
        return jobview

    def test_total_still_counts_terminal_tasks(self):
        """total() 保持全量语义(供作业视图展示),包含已完成任务。"""
        jobview = self._build_jobview_with_stale_tasks()
        self.assertEqual(jobview.total(), 8)

    def test_pending_total_excludes_terminal_tasks(self):
        """pending_total() 只数未终态任务,不受跨批次残留影响。"""
        jobview = self._build_jobview_with_stale_tasks()
        self.assertEqual(jobview.pending_total(), 2)

    def test_pending_total_excludes_failed_tasks(self):
        """失败任务同为终态,不应计入待处理数。"""
        jobview = JobManager()
        failed_task = make_task(1)
        waiting_task = make_task(2)
        assert jobview.add_task(failed_task)
        assert jobview.add_task(waiting_task)
        jobview.fail_task(failed_task)
        self.assertEqual(jobview.pending_total(), 1)

    def test_pending_total_counts_running_tasks(self):
        """运行中的任务属于本批,必须计入。"""
        jobview = JobManager()
        running_task = make_task(1)
        assert jobview.add_task(running_task)
        jobview.running_task(running_task)
        self.assertEqual(jobview.pending_total(), 1)


if __name__ == "__main__":
    unittest.main()
