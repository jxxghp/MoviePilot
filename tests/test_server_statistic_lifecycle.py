"""服务端统计兼容入口的后台任务生命周期回归。"""

import asyncio

from unittest.mock import Mock, patch

from app.adapters.external.server import MoviePilotServerHelper
from app.runtime.config import global_vars
from app.runtime.tasks import TaskRegistry


def test_legacy_subscription_reports_use_owned_threadsafe_tasks() -> None:
    """旧同步统计入口应保留 ABI，并让宿主等待已开始的线程工作。"""
    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    registry = Mock()

    def submit(coroutine, **_kwargs):
        """关闭测试协程，避免替身提交留下未等待警告。"""
        coroutine.close()
        return Mock()

    registry.submit_threadsafe.side_effect = submit
    with patch.object(global_vars, "CURRENT_EVENT_LOOP", loop), patch(
        "app.adapters.external.server.get_task_registry", return_value=registry
    ):
        assert MoviePilotServerHelper.sub_reg_async({"media_id": "1"}) is True
        assert MoviePilotServerHelper.sub_done_async({"media_id": "1"}) is True

    assert registry.submit_threadsafe.call_count == 2
    assert registry.submit_threadsafe.call_args_list[0].kwargs == {
        "loop": loop,
        "owner": "compat.server.subscribe_added_report",
        "cancel_on_shutdown": False,
    }
    assert registry.submit_threadsafe.call_args_list[1].kwargs == {
        "loop": loop,
        "owner": "compat.server.subscribe_done_report",
        "cancel_on_shutdown": False,
    }


def test_legacy_subscription_report_rejects_without_runtime_loop() -> None:
    """宿主生命周期不可用时应拒绝提交，并保持布尔返回合同。"""
    with patch.object(global_vars, "CURRENT_EVENT_LOOP", None):
        assert MoviePilotServerHelper.sub_done_async({"media_id": "1"}) is False


def test_legacy_subscription_report_handles_closed_task_registry() -> None:
    """运行 loop 尚在但宿主已封口时，兼容入口应保持布尔失败合同。"""
    registry = TaskRegistry()
    asyncio.run(registry.shutdown(timeout_seconds=0.01))
    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})

    with patch.object(global_vars, "CURRENT_EVENT_LOOP", loop), patch(
        "app.adapters.external.server.get_task_registry", return_value=registry
    ), patch("app.adapters.external.server.logger") as logger:
        assert MoviePilotServerHelper.sub_done_async({"media_id": "1"}) is False

    logger.warning.assert_called_once()
    assert "正在关闭" in logger.warning.call_args.args[0]
