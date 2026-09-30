import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

# 可选三方依赖在 CI / 全新环境可能未安装，补占位避免 app.modules.feishu 导入失败
ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.modules.feishu.feishu import Feishu  # noqa: E402
from app.modules.feishu.longconn import FeishuLongConnection  # noqa: E402
from app.modules.feishu.openapi import FeishuOpenApi  # noqa: E402

# 停止单个配置实例的耗时上限；远小于 Feishu._ws_join_timeout_seconds，证明停止不是靠等待超时。
_STOP_BUDGET_SECONDS = 1.0


class _OfflineRuns:
    """记录离线长连接的运行情况：每个 FeishuLongConnection 在哪个线程运行、是否被请求停止。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._released = {}
        self.threads = {}
        self.stopped = set()

    def released(self, connection) -> threading.Event:
        """返回该长连接的释放信号。"""
        with self._lock:
            return self._released.setdefault(id(connection), threading.Event())

    def running(self, connection, timeout: float = 2) -> bool:
        """等待该长连接进入 run()。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if id(connection) in self.threads:
                    return True
            time.sleep(0.01)
        return False

    def release_all(self) -> None:
        """释放所有仍阻塞的 run()，避免用例失败时遗留线程。"""
        with self._lock:
            events = list(self._released.values())
        for event in events:
            event.set()


@pytest.fixture
def offline_runs():
    """
    让真实 FeishuLongConnection 以离线方式运行：run() 阻塞到本实例的 stop() 被调用，
    不换取连接地址也不建立 WebSocket，保证零真实出站；stop() 仍执行真实实现。
    """
    runs = _OfflineRuns()
    real_stop = FeishuLongConnection.stop

    def _offline_run(connection) -> None:
        with runs._lock:
            runs.threads[id(connection)] = threading.current_thread()
        runs.released(connection).wait(timeout=10)

    def _tracked_stop(connection) -> None:
        real_stop(connection)
        with runs._lock:
            runs.stopped.add(id(connection))
        runs.released(connection).set()

    with (
        patch.object(FeishuLongConnection, "run", _offline_run),
        patch.object(FeishuLongConnection, "stop", _tracked_stop),
    ):
        yield runs
    runs.release_all()


def _start_client(name: str) -> Feishu:
    """按配置名启动一个飞书实例；OpenAPI 为桩，长连接线程真实启动。"""
    with patch.object(Feishu, "_build_api_client", return_value=MagicMock(spec=FeishuOpenApi)):
        return Feishu(
            FEISHU_APP_ID=f"cli_{name}",
            FEISHU_APP_SECRET=f"secret_{name}",
            name=name,
        )


def test_config_instances_run_independent_long_connections(offline_runs):
    """多个飞书配置各自持有独立长连接并在各自线程运行。"""
    first, second = _start_client("feishu-a"), _start_client("feishu-b")
    try:
        assert offline_runs.running(first._ws_client)
        assert offline_runs.running(second._ws_client)

        assert first._ws_client is not second._ws_client
        assert offline_runs.threads[id(first._ws_client)] is first._ws_thread
        assert offline_runs.threads[id(second._ws_client)] is second._ws_thread
        assert first._ws_thread is not second._ws_thread
        assert first.get_state() and second.get_state()
    finally:
        first.stop()
        second.stop()


def test_stopping_one_instance_keeps_other_connected(offline_runs):
    """停止一个配置实例只结束它自己的长连接，另一实例继续运行。"""
    first, second = _start_client("feishu-a"), _start_client("feishu-b")
    try:
        assert offline_runs.running(first._ws_client)
        assert offline_runs.running(second._ws_client)

        started = time.monotonic()
        assert first.stop() is True
        elapsed = time.monotonic() - started

        assert elapsed < _STOP_BUDGET_SECONDS
        assert not first._ws_thread.is_alive()
        assert not first.get_state()
        assert id(second._ws_client) not in offline_runs.stopped
        assert second._ws_thread.is_alive()
        assert second.get_state()
    finally:
        second.stop()

    assert not second._ws_thread.is_alive()


def test_concurrent_stop_of_all_instances_returns_quickly(offline_runs):
    """关机时多个实例并发停止，均应在预算内确认线程退出。"""
    clients = [_start_client(f"feishu-{index}") for index in range(3)]
    for client in clients:
        assert offline_runs.running(client._ws_client)
    results = {}

    def _stop(client: Feishu) -> None:
        results[client._name] = client.stop()

    started = time.monotonic()
    stoppers = [threading.Thread(target=_stop, args=(client,)) for client in clients]
    for stopper in stoppers:
        stopper.start()
    for stopper in stoppers:
        stopper.join(timeout=5)
    elapsed = time.monotonic() - started

    assert results == {client._name: True for client in clients}
    assert elapsed < _STOP_BUDGET_SECONDS
    assert all(not client._ws_thread.is_alive() for client in clients)


def test_restart_after_stop_uses_fresh_long_connection(offline_runs):
    """停止后重新启动必须新建长连接：已停止的连接会立即退出，不能复用。"""
    client = _start_client("feishu-a")
    try:
        stopped_connection = client._ws_client
        assert offline_runs.running(stopped_connection)
        assert client.stop() is True

        client._start_ws_client()

        assert client._ws_client is not stopped_connection
        assert offline_runs.running(client._ws_client)
        assert client._ws_thread.is_alive()
    finally:
        assert client.stop() is True
