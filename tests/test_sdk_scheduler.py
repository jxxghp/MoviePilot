"""插件调度 SDK 的公开面测试。"""

import app.sdk.scheduler as scheduler_sdk


def test_scheduler_sdk_exposes_only_narrow_service_contracts() -> None:
    """新插件 SDK 不得泄漏 concrete Scheduler 或其可变运行状态。"""
    assert set(scheduler_sdk.__all__) == {
        "ScheduleInfo",
        "ScheduleProgress",
        "get_agent_task_next_run",
        "list_scheduler_jobs",
        "remove_agent_task_job",
        "remove_plugin_job",
        "start_agent_task",
        "start_scheduler_job",
        "update_agent_task_job",
        "update_plugin_job",
    }
    assert not hasattr(scheduler_sdk, "Scheduler")
    assert not hasattr(scheduler_sdk, "BackgroundScheduler")
    assert not hasattr(scheduler_sdk, "_jobs")
