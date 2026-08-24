import threading
import time
from unittest.mock import MagicMock

from app.application.messaging.message import MessageQueueManager, TemplateHelper, stop_message
from app.foundation.singleton import SingletonClass


def test_message_queue_stop_wakes_idle_monitor(monkeypatch):
    """消息队列停止时应唤醒空闲监控线程，不等待完整检查周期"""
    monkeypatch.setattr(MessageQueueManager, "init_config", lambda self: None)
    manager = object.__new__(MessageQueueManager)
    manager.__init__(check_interval=10)

    started_at = time.monotonic()
    converged = manager.stop()
    elapsed = time.monotonic() - started_at

    assert converged is True
    assert elapsed < 1
    assert not manager.thread.is_alive()


def test_message_queue_stop_reports_blocked_callback_and_supports_retry(monkeypatch):
    """发送回调仍阻塞时应有限返回 False，并保留线程供后续重试。"""
    entered = threading.Event()
    release = threading.Event()

    def blocked_send_callback(*_args, **_kwargs) -> None:
        """模拟无法由消息队列强制取消的同步渠道调用。"""
        entered.set()
        release.wait()

    monkeypatch.setattr(MessageQueueManager, "init_config", lambda self: None)
    manager = object.__new__(MessageQueueManager)
    manager.__init__(send_callback=blocked_send_callback, check_interval=0)
    manager.queue.put({"args": ("payload",), "kwargs": {}})

    assert entered.wait(timeout=1)
    try:
        started_at = time.monotonic()
        assert manager.stop(timeout=0.01) is False
        assert time.monotonic() - started_at < 1
        assert manager.thread.is_alive()
    finally:
        release.set()

    assert manager.stop(timeout=1) is True
    assert not manager.thread.is_alive()


def test_stop_message_does_not_initialize_absent_services(monkeypatch):
    """消息服务未初始化时，关闭入口不应为了清理而创建后台资源"""
    monkeypatch.setattr(SingletonClass, "_instances", {})

    assert MessageQueueManager.get_existing_instance() is None
    assert TemplateHelper.get_existing_instance() is None
    assert stop_message() is True

    assert MessageQueueManager not in SingletonClass._instances
    assert TemplateHelper not in SingletonClass._instances


def test_stop_message_aggregates_queue_failure_and_closes_template(monkeypatch):
    """消息队列未收敛时仍应关闭模板缓存并向生命周期返回 False。"""
    queue_manager = MagicMock()
    queue_manager.stop.return_value = False
    template_helper = MagicMock()
    monkeypatch.setattr(
        SingletonClass,
        "_instances",
        {
            MessageQueueManager: queue_manager,
            TemplateHelper: template_helper,
        },
    )

    assert stop_message(timeout=0.25) is False

    queue_manager.stop.assert_called_once_with(timeout=0.25)
    template_helper.close.assert_called_once_with()
