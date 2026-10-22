"""
验证异步日志容量与事件队列深度观测的实际运行路径。
"""

import threading

from app.runtime import events as event_module
from app.runtime import log as log_module
from app.runtime.events import Event, EventManager
from tests.test_subscription_logging import fixture_file_writer  # noqa: F401


def test_subscription_batch_bounds_handlers_and_reclaims_idle(file_writer, tmp_path, monkeypatch):
    """
    批量写入44条订阅时句柄不超过32，时钟推进后释放旧句柄而保留新日志。
    """
    now = [100.0]
    monkeypatch.setattr(log_module.time, "monotonic", lambda: now[0])
    entries = [log_module.LogEntry("INFO", "payload", tmp_path / "subscriptions" / f"{i}.log")
               for i in range(1, 45)]
    file_writer._write_batch(entries)
    assert len(file_writer._rotating_handlers) == 32
    assert len(file_writer._scoped_handler_times) == 32
    now[0] += 61
    path = tmp_path / "subscriptions" / "45.log"
    file_writer._write_batch([log_module.LogEntry("INFO", "latest", path)])
    assert set(file_writer._rotating_handlers) == {path}
    assert "latest" in path.read_text()


def test_subscription_batch_trims_directory_quota(file_writer, tmp_path, monkeypatch):
    """
    异步批量路径执行目录配额清理，仅删除旧订阅日志而保留非日志文件。
    """
    directory = tmp_path / "subscriptions"
    directory.mkdir()
    for name in ("1.log", "1.log.1", "notes.txt"):
        (directory / name).write_text("x" * 100)
    monkeypatch.setattr(log_module, "_SCOPED_LOG_TOTAL_BYTES", 50)
    active = directory / "2.log"
    file_writer._write_batch([log_module.LogEntry("INFO", "latest", active)])
    assert not (directory / "1.log").exists()
    assert (directory / "notes.txt").exists()
    assert "latest" in active.read_text()


def test_broadcast_depth_is_balanced_after_real_consumer_and_sentinel(monkeypatch):
    """
    累加型队列指标只记录入队加一和出队减一，真实消费者处理后回零且不计哨兵。
    """
    manager = object.__new__(EventManager)
    EventManager.__init__(manager)
    deltas = []
    processed = []

    def metric(name, value, **_labels):
        """
        模拟UpDownCounter的累加语义。
        """
        if name == "event.queue.depth":
            deltas.append(value)

    monkeypatch.setattr(event_module, "record_metric", metric)
    monkeypatch.setattr(manager, "_EventManager__dispatch_broadcast_event", processed.append)
    events = [Event("review-depth", {"value": index}, priority=index) for index in range(3)]
    for event in events:
        manager._EventManager__trigger_broadcast_event(event)
    manager._EventManager__event_queue.put((999, event_module._EVENT_STOP_SENTINEL))
    manager._EventManager__event.set()
    thread = threading.Thread(target=manager._EventManager__broadcast_consumer_loop)
    try:
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert processed == events
        assert deltas == [1, 1, 1, -1, -1, -1]
        assert sum(deltas) == 0
    finally:
        manager._EventManager__event.clear()
        thread.join(5)
