import threading
import time
from unittest.mock import MagicMock

from app.application.messaging.message import (
    MessageHelper,
    MessageQueueManager,
    TemplateHelper,
    stop_message,
)
from app.foundation.singleton import Singleton, SingletonClass


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
    assert manager.thread is None


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
        assert manager.thread is not None
        assert manager.thread.is_alive()
    finally:
        release.set()

    assert manager.stop(timeout=1) is True
    assert manager.thread is None


def test_stop_message_does_not_initialize_absent_services(monkeypatch):
    """消息服务未初始化时，关闭入口不应为了清理而创建后台资源"""
    monkeypatch.setattr(SingletonClass, "_instances", {})
    monkeypatch.setattr(Singleton, "_instances", {})

    assert MessageQueueManager.get_existing_instance() is None
    assert TemplateHelper.get_existing_instance() is None
    assert MessageHelper.get_existing_instance() is None
    assert stop_message() is True

    assert MessageQueueManager not in SingletonClass._instances
    assert TemplateHelper not in SingletonClass._instances


def test_explicit_queue_can_restart_after_converged_stop(monkeypatch) -> None:
    """显式 owner 关闭并释放后，下一轮 lifespan 应创建并启动新线程。"""
    monkeypatch.setattr(SingletonClass, "_instances", {})
    monkeypatch.setattr(MessageQueueManager, "init_config", lambda self: None)
    first = MessageQueueManager(auto_start=False)
    assert first.thread is None

    first.start()
    assert first.thread is not None and first.thread.is_alive()
    assert stop_message(queue_manager=first) is True
    assert MessageQueueManager.get_existing_instance() is None

    second = MessageQueueManager(auto_start=False)
    assert second is not first
    second.start()
    assert second.thread is not None and second.thread.is_alive()
    assert stop_message(queue_manager=second) is True


def test_bound_clients_keep_distinct_chain_callbacks_without_thread(monkeypatch) -> None:
    """轻量客户端不得启动线程，也不得把首个 Chain 回调写进共享 owner。"""
    monkeypatch.setattr(SingletonClass, "_instances", {})
    monkeypatch.setattr(MessageQueueManager, "init_config", lambda self: None)
    manager = MessageQueueManager(auto_start=False)
    first = MagicMock()
    second = MagicMock()

    manager.bind(first).send_message("first", immediately=True)
    manager.bind(second).send_message("second", immediately=True)

    assert manager.thread is None
    assert manager.send_callback is None
    first.assert_called_once_with("first")
    second.assert_called_once_with("second")
    assert stop_message(queue_manager=manager) is True


def test_stop_message_closes_and_releases_explicit_message_helper(monkeypatch) -> None:
    """消息通知缓存收敛后必须释放单例，避免下一轮复用已关闭缓存。"""
    monkeypatch.setattr(Singleton, "_instances", {})
    helper = MessageHelper()
    close = MagicMock()
    monkeypatch.setattr(helper._recent_notification_keys, "close", close)

    assert stop_message(message_helper=helper) is True

    close.assert_called_once_with()
    assert MessageHelper.get_existing_instance() is None
    assert MessageHelper() is not helper


def test_stop_message_discovers_existing_helper_and_only_closes_it_once(monkeypatch) -> None:
    """无显式 owner 时应发现既存通知缓存，重复关闭不得再次处理旧实例。"""
    monkeypatch.setattr(Singleton, "_instances", {})
    helper = MessageHelper()
    close = MagicMock()
    monkeypatch.setattr(helper._recent_notification_keys, "close", close)

    assert stop_message() is True
    assert stop_message() is True

    close.assert_called_once_with()
    assert MessageHelper.get_existing_instance() is None


def test_stop_message_does_not_release_replacement_helper(monkeypatch) -> None:
    """旧缓存关闭期间若发布了新 owner，身份校验不得误删新实例。"""
    monkeypatch.setattr(Singleton, "_instances", {})
    old_helper = MessageHelper()
    replacements = []

    def replace_owner() -> None:
        """模拟关闭回调先释放旧身份，再发布下一轮 lifespan 的 owner。"""
        assert MessageHelper.release_existing_instance(old_helper) is True
        replacements.append(MessageHelper())

    monkeypatch.setattr(old_helper._recent_notification_keys, "close", replace_owner)

    assert stop_message(message_helper=old_helper) is True

    assert len(replacements) == 1
    assert MessageHelper.get_existing_instance() is replacements[0]


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
