import time

from app.helper.message import MessageQueueManager, TemplateHelper, stop_message
from app.utils.singleton import SingletonClass


def test_message_queue_stop_wakes_idle_monitor(monkeypatch):
    """消息队列停止时应唤醒空闲监控线程，不等待完整检查周期"""
    monkeypatch.setattr(MessageQueueManager, "init_config", lambda self: None)
    manager = object.__new__(MessageQueueManager)
    manager.__init__(check_interval=10)

    started_at = time.monotonic()
    manager.stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1
    assert not manager.thread.is_alive()


def test_stop_message_does_not_initialize_absent_services(monkeypatch):
    """消息服务未初始化时，关闭入口不应为了清理而创建后台资源"""
    monkeypatch.setattr(SingletonClass, "_instances", {})

    assert MessageQueueManager.get_existing_instance() is None
    assert TemplateHelper.get_existing_instance() is None
    stop_message()

    assert MessageQueueManager not in SingletonClass._instances
    assert TemplateHelper not in SingletonClass._instances
