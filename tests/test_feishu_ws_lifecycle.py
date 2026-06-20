import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.testing.bootstrap import ensure_optional_stub

# 可选三方依赖在 CI / 全新环境可能未安装，补占位避免 app.modules.feishu 导入失败
ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.modules.feishu.feishu import Feishu


def _build_feishu_client() -> Feishu:
    """构造不会启动真实飞书长连接的测试客户端。"""
    with (
        patch.object(Feishu, "_build_api_client", return_value=MagicMock()),
        patch.object(Feishu, "_start_ws_client"),
    ):
        return Feishu(
            FEISHU_APP_ID="cli_test_app_id",
            FEISHU_APP_SECRET="cli_test_app_secret",
            name="feishu-test",
        )


async def _wait_forever() -> None:
    """模拟飞书 SDK 创建的长生命周期后台任务。"""
    await asyncio.Future()


def test_shutdown_ws_client_cancels_sdk_tasks_before_quiet_disconnect():
    """飞书关机清理应先消费后台任务，再静默关闭 WebSocket 连接。"""
    client = _build_feishu_client()
    loop = asyncio.new_event_loop()
    closed = False

    async def _close_conn() -> None:
        """记录测试连接已被关闭。"""
        nonlocal closed
        closed = True

    try:
        asyncio.set_event_loop(loop)
        task = loop.create_task(_wait_forever())
        task.add_done_callback(client._consume_ws_task_result)
        client._ws_tasks.add(task)
        ws_client = SimpleNamespace(
            _auto_reconnect=True,
            _conn=SimpleNamespace(close=_close_conn),
            _conn_url="wss://msg-frontier.feishu.cn/ws/v2?access_key=secret&ticket=secret",
            _conn_id="conn_test",
            _service_id="service_test",
            _lock=asyncio.Lock(),
        )
        client._ws_client = ws_client

        loop.run_until_complete(client._shutdown_ws_client())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert task.cancelled()
    assert closed
    assert not ws_client._auto_reconnect
    assert ws_client._conn is None
    assert ws_client._conn_url == ""
    assert ws_client._conn_id == ""
    assert ws_client._service_id == ""
    assert client._ws_tasks == set()


def test_shutdown_ws_client_skips_disconnect_when_sdk_lock_is_busy_and_connection_gone():
    """SDK 已无连接对象时，关机清理不应等待可能长期占用的内部锁。"""
    client = _build_feishu_client()

    async def _run_shutdown() -> SimpleNamespace:
        lock = asyncio.Lock()
        await lock.acquire()
        ws_client = SimpleNamespace(
            _auto_reconnect=True,
            _conn=None,
            _conn_url="wss://msg-frontier.feishu.cn/ws/v2?access_key=secret&ticket=secret",
            _conn_id="conn_test",
            _service_id="service_test",
            _lock=lock,
        )
        client._ws_client = ws_client

        await asyncio.wait_for(client._shutdown_ws_client(), timeout=0.2)
        return ws_client

    ws_client = asyncio.run(_run_shutdown())

    assert not ws_client._auto_reconnect
    assert ws_client._conn is None
    assert ws_client._conn_url == ""
    assert ws_client._conn_id == ""
    assert ws_client._service_id == ""


def test_consume_ws_task_result_suppresses_stop_exception():
    """停止过程中飞书 SDK 后台任务的异常应被取回并降为调试日志。"""
    client = _build_feishu_client()
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        future.set_exception(RuntimeError("normal shutdown"))
        client._ws_tasks.add(future)
        client._stop_event.set()

        with (
            patch("app.modules.feishu.feishu.logger.debug") as debug_logger,
            patch("app.modules.feishu.feishu.logger.error") as error_logger,
        ):
            client._consume_ws_task_result(future)
    finally:
        loop.close()

    debug_logger.assert_called_once()
    error_logger.assert_not_called()
    assert future not in client._ws_tasks
