import asyncio
import json
import threading
import time
from unittest.mock import Mock, patch

import pytest
from telebot import TeleBot

from app.modules import _MessageBase
from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.modules.feishu import FeishuModule
from app.modules.feishu.feishu import Feishu
from app.modules.filter import FilterModule
from app.modules.plex import PlexModule
from app.modules.qqbot import gateway as qq_gateway
from app.modules.qqbot.module import QQBotModule
from app.modules.qqbot.qqbot import QQBot
from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.modules.telegram.module import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.modules.themoviedb import TheMovieDbModule
from app.modules.trimemedia.module import TrimeMediaModule
from app.modules.ugreen.module import UgreenModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.runtime.execution import run_in_threadpool_to_completion


def test_config_reload_stops_before_initializing_latest_generation():
    """同一模块的重载必须串行，并依次停止和初始化 generation。"""
    module = TelegramModule()
    call_order = []
    reload_started = threading.Event()
    reload_finished = threading.Event()

    def reload_module():
        reload_started.set()
        module.on_config_changed()
        reload_finished.set()

    with patch.object(
        module, "stop", side_effect=lambda: call_order.append("stop")
    ), patch.object(
        _MessageBase,
        "init_service",
        side_effect=lambda **_kwargs: call_order.append("init"),
    ):
        module._reload_lock.acquire()
        try:
            reload_thread = threading.Thread(target=reload_module)
            reload_thread.start()
            assert reload_started.wait(1)
            assert not reload_finished.wait(0.1)
        finally:
            module._reload_lock.release()

        assert reload_finished.wait(1)
        reload_thread.join()

    assert call_order == ["stop", "init"]


def test_initialization_does_not_stop_a_fresh_module_generation():
    """首次初始化只创建资源，停止旧 generation 由重载入口负责。"""
    module = TelegramModule()

    with patch.object(module, "stop") as stop, patch.object(
        _MessageBase, "init_service"
    ) as init_service:
        module.init_module()

    stop.assert_not_called()
    init_service.assert_called_once()


def test_config_reload_initializes_latest_generation_after_stop_failure():
    """旧资源停止异常只记录错误，不阻止最新配置完成初始化。"""
    module = TelegramModule()
    call_order = []

    def stop_with_failure():
        call_order.append("stop")
        raise RuntimeError("stop failed")

    with patch.object(
        module,
        "stop",
        side_effect=stop_with_failure,
    ), patch.object(
        _MessageBase,
        "init_service",
        side_effect=lambda **_kwargs: call_order.append("init"),
    ):
        module.on_config_changed()

    assert call_order == ["stop", "init"]


def test_tmdb_reload_closes_old_client_when_cache_save_fails():
    """TMDB 缓存保存失败时仍须关闭旧客户端并初始化最新配置。"""
    module = TheMovieDbModule()
    module.cache = Mock()
    module.cache.save.side_effect = OSError("cache write failed")
    module.tmdb = Mock()

    with patch.object(module, "init_module") as init_module:
        module.on_config_changed()

    module.tmdb.close.assert_called_once_with()
    init_module.assert_called_once_with()


def test_filter_reload_uses_shared_module_lifecycle_lock():
    """过滤规则重载必须经过模块基类的串行 stop 和 init。"""
    module = FilterModule()
    reload_started = threading.Event()
    reload_finished = threading.Event()
    call_order = []

    def reload_module():
        reload_started.set()
        module.on_config_changed()
        reload_finished.set()

    with patch(
        "app.modules.filter.clear_rust_parse_options_cache",
        side_effect=lambda: call_order.append("stop"),
    ), patch.object(
        module, "init_module", side_effect=lambda: call_order.append("init")
    ):
        module._reload_lock.acquire()
        try:
            reload_thread = threading.Thread(target=reload_module)
            reload_thread.start()
            assert reload_started.wait(1)
            assert not reload_finished.wait(0.1)
        finally:
            module._reload_lock.release()

        assert reload_finished.wait(1)
        reload_thread.join()

    assert call_order == ["stop", "init"]


@pytest.mark.parametrize(
    ("module_type", "stop_method", "requires_authentication"),
    [
        (DiscordModule, "stop", False),
        (FeishuModule, "stop", False),
        (QQBotModule, "stop", False),
        (SlackModule, "stop", False),
        (TelegramModule, "stop", False),
        (WechatModule, "stop", False),
        (WechatClawBotModule, "stop", False),
        (PlexModule, "close", False),
        (TrimeMediaModule, "disconnect", True),
        (UgreenModule, "disconnect", True),
    ],
)
def test_module_stop_isolates_each_service_instance(
    module_type, stop_method, requires_authentication
):
    """单个服务停止失败时必须继续关闭同模块的其余实例。"""
    module = module_type()
    failed_client = Mock()
    healthy_client = Mock()
    getattr(failed_client, stop_method).side_effect = RuntimeError("stop failed")
    if requires_authentication:
        failed_client.is_authenticated.return_value = True
        healthy_client.is_authenticated.return_value = True
    module._instances = {"failed": failed_client, "healthy": healthy_client}

    module.stop()

    getattr(failed_client, stop_method).assert_called_once_with()
    getattr(healthy_client, stop_method).assert_called_once_with()


def test_telegram_stop_closes_sdk_and_waits_for_polling_thread():
    """客户端停止完成后不得保留 SDK worker 或 polling 线程句柄。"""
    client = Telegram.__new__(Telegram)
    bot = Mock()
    bot.threaded = False
    bot.worker_pool = None
    client._bot = bot
    polling_thread = Mock()
    polling_thread.is_alive.side_effect = [True, False]
    client._polling_thread = polling_thread
    client._typing_tasks = {}
    client._typing_stop_flags = {}
    client._typing_lock = threading.RLock()
    client._typing_lifecycle_lock = threading.RLock()
    client._typing_accepting = True

    assert client.stop() is True
    assert client.stop() is True

    bot.stop_polling.assert_called_once_with()
    polling_thread.join.assert_called_once_with(
        timeout=pytest.approx(client._shutdown_timeout_seconds, abs=0.1)
    )
    assert client._bot is None
    assert client._polling_thread is None


def test_telegram_stop_keeps_polling_owner_when_thread_misses_deadline():
    """polling 超过关闭预算时必须返回未收敛并保留原 owner。"""
    client = Telegram.__new__(Telegram)
    bot = Mock()
    bot.threaded = False
    bot.worker_pool = None
    polling_thread = Mock()
    polling_thread.is_alive.return_value = True
    client._bot = bot
    client._polling_thread = polling_thread
    client._shutdown_timeout_seconds = 0.01
    client._typing_tasks = {}
    client._typing_stop_flags = {}
    client._typing_lock = threading.RLock()
    client._typing_lifecycle_lock = threading.RLock()
    client._typing_accepting = True

    assert client.stop() is False

    polling_thread.join.assert_called_once()
    remaining_timeout = polling_thread.join.call_args.kwargs["timeout"]
    assert 0 <= remaining_timeout <= client._shutdown_timeout_seconds
    assert client._bot is bot
    assert client._polling_thread is polling_thread


@pytest.mark.asyncio
async def test_telegram_stop_bounds_real_sdk_worker_and_retries_after_release():
    """真实 SDK worker 阻塞时应保留 owner，释放后重试可以完整收敛。"""
    bot = TeleBot("123:test", threaded=True, num_threads=1)
    entered = threading.Event()
    release = threading.Event()

    def blocking_callback() -> None:
        entered.set()
        release.wait(timeout=1.0)

    bot.worker_pool.put(blocking_callback)
    assert await asyncio.to_thread(entered.wait, 0.2)

    client = Telegram.__new__(Telegram)
    client._bot = bot
    client._polling_thread = None
    client._shutdown_timeout_seconds = 0.02
    client._typing_tasks = {}
    client._typing_stop_flags = {}
    client._typing_lock = threading.RLock()
    client._typing_lifecycle_lock = threading.RLock()
    client._typing_accepting = True

    heartbeat = asyncio.create_task(asyncio.sleep(0.005))
    started_at = time.monotonic()
    try:
        assert await run_in_threadpool_to_completion(client.stop) is False
        assert time.monotonic() - started_at < 0.2
        assert heartbeat.done()
        assert client._bot is bot
        assert any(worker.is_alive() for worker in bot.worker_pool.workers)

        release.set()
        for worker in bot.worker_pool.workers:
            await asyncio.to_thread(worker.join, 0.2)

        assert await run_in_threadpool_to_completion(client.stop) is True
        assert client._bot is None
        assert client._polling_thread is None
    finally:
        release.set()
        for worker in bot.worker_pool.workers:
            worker.stop()
            await asyncio.to_thread(worker.join, 0.2)


@pytest.mark.parametrize(
    "module_type",
    [
        DiscordModule,
        FeishuModule,
        QQBotModule,
        SlackModule,
        TelegramModule,
        WechatModule,
        WechatClawBotModule,
    ],
)
def test_message_channel_module_reports_nonconverging_instance_after_peers(
    module_type,
):
    """渠道单实例未收敛时必须继续停止其余实例并返回 False。"""
    module = module_type()
    blocked_client = Mock()
    blocked_client.stop.return_value = False
    healthy_client = Mock()
    healthy_client.stop.return_value = True
    module._instances = {
        "blocked": blocked_client,
        "healthy": healthy_client,
    }

    assert module.stop() is False
    blocked_client.stop.assert_called_once_with()
    healthy_client.stop.assert_called_once_with()


def test_qqbot_stop_retains_gateway_thread_until_retry() -> None:
    """QQ Gateway 超时后不得把活线程伪装成已收敛。"""
    client = QQBot.__new__(QQBot)
    client._gateway_stop = threading.Event()
    client._gateway_ws_holder = []
    gateway_thread = Mock()
    gateway_thread.is_alive.return_value = True
    client._gateway_thread = gateway_thread
    client._gateway_join_timeout_seconds = 0.01

    assert client.stop() is False
    assert client._gateway_thread is gateway_thread

    gateway_thread.is_alive.return_value = False
    assert client.stop() is True


def test_qqbot_stop_retains_gateway_until_inflight_heartbeat_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳发送仍阻塞时 Gateway 必须保持存活，释放后才允许关闭成功。"""
    heartbeat_started = threading.Event()
    release_heartbeat = threading.Event()
    connection_closed = threading.Event()
    callbacks = {}
    fake_ws = Mock()
    hello_payload = json.dumps({"op": 10, "d": {"heartbeat_interval": 1}})

    def build_websocket(_url, **kwargs):
        """保存 Gateway 回调并返回隔离的 WebSocket 桩。"""
        callbacks.update(kwargs)
        return fake_ws

    def send(payload: str) -> None:
        """Identify 立即完成，首个心跳保持在发送中的故障状态。"""
        if json.loads(payload).get("op") == 1:
            heartbeat_started.set()
            release_heartbeat.wait()

    def close() -> None:
        """同步触发 close 回调，复现 QQBot.stop() 的真实调用线程。"""
        callbacks["on_close"](fake_ws, 1000, "test close")
        connection_closed.set()

    def run_forever(**_kwargs) -> None:
        """发送 Hello 后保持连接，直到 stop() 主动关闭。"""
        callbacks["on_message"](fake_ws, hello_payload)
        connection_closed.wait()

    fake_ws.send.side_effect = send
    fake_ws.close.side_effect = close
    fake_ws.run_forever.side_effect = run_forever
    monkeypatch.setattr(qq_gateway.websocket, "WebSocketApp", build_websocket)

    client = QQBot.__new__(QQBot)
    client._gateway_stop = threading.Event()
    client._gateway_ws_holder = []
    client._gateway_join_timeout_seconds = 0.02
    gateway_thread = threading.Thread(
        target=qq_gateway.run_gateway,
        kwargs={
            "app_id": "app-id",
            "app_secret": "secret",
            "config_name": "test",
            "get_token_fn": lambda _app_id, _secret: "token",
            "get_gateway_url_fn": lambda _token: "wss://gateway.test",
            "on_message_fn": lambda _payload: None,
            "stop_event": client._gateway_stop,
            "ws_holder": client._gateway_ws_holder,
        },
        daemon=True,
    )
    client._gateway_thread = gateway_thread
    gateway_thread.start()

    try:
        assert heartbeat_started.wait(0.5)
        started_at = time.monotonic()
        assert client.stop() is False
        assert time.monotonic() - started_at < 0.2
        assert gateway_thread.is_alive()

        release_heartbeat.set()
        gateway_thread.join(timeout=1.0)
        assert client.stop() is True
        assert not gateway_thread.is_alive()
    finally:
        client._gateway_stop.set()
        release_heartbeat.set()
        connection_closed.set()
        gateway_thread.join(timeout=1.0)


def test_qq_gateway_replaces_heartbeat_generation_without_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复 Hello 必须等待旧心跳终止，任何时刻只能有一个发送 generation。"""
    first_heartbeat_started = threading.Event()
    release_first_heartbeat = threading.Event()
    second_heartbeat_started = threading.Event()
    release_second_heartbeat = threading.Event()
    third_heartbeat_started = threading.Event()
    second_hello_entered = threading.Event()
    third_hello_entered = threading.Event()
    connection_ready = threading.Event()
    connection_closed = threading.Event()
    counters_lock = threading.Lock()
    callbacks = {}
    fake_ws = Mock()
    hello_payload = json.dumps({"op": 10, "d": {"heartbeat_interval": 1}})
    heartbeat_count = 0
    active_heartbeats = 0
    max_active_heartbeats = 0

    def build_websocket(_url, **kwargs):
        """保存 Gateway 回调并返回隔离的 WebSocket 桩。"""
        callbacks.update(kwargs)
        return fake_ws

    def send(payload: str) -> None:
        """阻塞第一代心跳，并记录是否出现跨 generation 并发发送。"""
        nonlocal heartbeat_count, active_heartbeats, max_active_heartbeats
        if json.loads(payload).get("op") != 1:
            return
        with counters_lock:
            heartbeat_count += 1
            current_heartbeat = heartbeat_count
            active_heartbeats += 1
            max_active_heartbeats = max(
                max_active_heartbeats,
                active_heartbeats,
            )
        try:
            if current_heartbeat == 1:
                first_heartbeat_started.set()
                release_first_heartbeat.wait()
            elif current_heartbeat == 2:
                second_heartbeat_started.set()
                release_second_heartbeat.wait()
            elif current_heartbeat == 3:
                third_heartbeat_started.set()
        finally:
            with counters_lock:
                active_heartbeats -= 1

    def run_forever(**_kwargs) -> None:
        """发送首个 Hello 后保持连接，第二个 Hello 由测试线程注入。"""
        callbacks["on_message"](fake_ws, hello_payload)
        connection_ready.set()
        connection_closed.wait()

    def send_second_hello() -> None:
        """从独立调用线程注入重复 Hello，以观测 generation 屏障。"""
        second_hello_entered.set()
        callbacks["on_message"](fake_ws, hello_payload)

    def send_third_hello() -> None:
        """在第二代发送中再次注入 Hello，供 close 失效待发布 generation。"""
        third_hello_entered.set()
        callbacks["on_message"](fake_ws, hello_payload)

    fake_ws.send.side_effect = send
    fake_ws.run_forever.side_effect = run_forever
    monkeypatch.setattr(qq_gateway.websocket, "WebSocketApp", build_websocket)

    stop_event = threading.Event()
    ws_holder = []
    gateway_thread = threading.Thread(
        target=qq_gateway.run_gateway,
        kwargs={
            "app_id": "app-id",
            "app_secret": "secret",
            "config_name": "generation-test",
            "get_token_fn": lambda _app_id, _secret: "token",
            "get_gateway_url_fn": lambda _token: "wss://gateway.test",
            "on_message_fn": lambda _payload: None,
            "stop_event": stop_event,
            "ws_holder": ws_holder,
        },
        daemon=True,
    )
    gateway_thread.start()
    hello_thread = threading.Thread(target=send_second_hello, daemon=True)
    hello_thread_started = False
    third_hello_thread = threading.Thread(target=send_third_hello, daemon=True)
    third_hello_thread_started = False

    try:
        assert connection_ready.wait(0.5)
        assert first_heartbeat_started.wait(0.5)
        hello_thread.start()
        hello_thread_started = True
        assert second_hello_entered.wait(0.2)
        assert not second_heartbeat_started.wait(0.03)
        assert hello_thread.is_alive()
        assert max_active_heartbeats == 1

        release_first_heartbeat.set()
        hello_thread.join(timeout=1.0)
        assert not hello_thread.is_alive()
        assert second_heartbeat_started.wait(0.5)
        assert max_active_heartbeats == 1

        third_hello_thread.start()
        third_hello_thread_started = True
        assert third_hello_entered.wait(0.2)
        assert third_hello_thread.is_alive()
        close_started_at = time.monotonic()
        callbacks["on_close"](fake_ws, 1000, "close during replacement")
        assert time.monotonic() - close_started_at < 0.2

        release_second_heartbeat.set()
        third_hello_thread.join(timeout=1.0)
        assert not third_hello_thread.is_alive()
        assert not third_heartbeat_started.wait(0.05)
        assert max_active_heartbeats == 1

        stop_event.set()
        connection_closed.set()
        gateway_thread.join(timeout=1.0)
        assert not gateway_thread.is_alive()
    finally:
        stop_event.set()
        release_first_heartbeat.set()
        release_second_heartbeat.set()
        connection_closed.set()
        if hello_thread_started:
            hello_thread.join(timeout=1.0)
        if third_hello_thread_started:
            third_hello_thread.join(timeout=1.0)
        gateway_thread.join(timeout=1.0)


def test_qq_gateway_reconnect_joins_old_heartbeat_and_ignores_stale_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重连必须先回收旧心跳，迟到的旧连接 close 不得终止新 generation。"""
    first_callbacks = {}
    second_callbacks = {}
    first_ws = Mock()
    second_ws = Mock()
    first_heartbeat_started = threading.Event()
    release_first_heartbeat = threading.Event()
    return_first_connection = threading.Event()
    second_connection_created = threading.Event()
    second_heartbeat_started = threading.Event()
    release_second_heartbeat = threading.Event()
    second_followup_started = threading.Event()
    return_second_connection = threading.Event()
    hello_payload = json.dumps({"op": 10, "d": {"heartbeat_interval": 1}})
    websocket_count = 0
    second_heartbeat_count = 0

    def build_websocket(_url, **kwargs):
        """依次创建两条连接，并保留各自回调以注入迟到 close。"""
        nonlocal websocket_count
        websocket_count += 1
        if websocket_count == 1:
            first_callbacks.update(kwargs)
            return first_ws
        if websocket_count == 2:
            second_callbacks.update(kwargs)
            second_connection_created.set()
            return second_ws
        raise AssertionError("Gateway 在停止后不应建立第三条测试连接")

    def send_first(payload: str) -> None:
        """阻塞第一条连接的心跳，供重连屏障检查。"""
        if json.loads(payload).get("op") == 1:
            first_heartbeat_started.set()
            release_first_heartbeat.wait()

    def send_second(payload: str) -> None:
        """阻塞新连接首个心跳，并记录迟到 close 后是否继续工作。"""
        nonlocal second_heartbeat_count
        if json.loads(payload).get("op") != 1:
            return
        second_heartbeat_count += 1
        if second_heartbeat_count == 1:
            second_heartbeat_started.set()
            release_second_heartbeat.wait()
        else:
            second_followup_started.set()

    def run_first(**_kwargs) -> None:
        """第一条连接握手后按测试信号返回，触发真实 reconnect 路径。"""
        first_callbacks["on_message"](first_ws, hello_payload)
        return_first_connection.wait()

    def run_second(**_kwargs) -> None:
        """第二条连接保持运行，直到测试完成迟到 close 校验。"""
        second_callbacks["on_message"](second_ws, hello_payload)
        return_second_connection.wait()

    first_ws.send.side_effect = send_first
    first_ws.run_forever.side_effect = run_first
    second_ws.send.side_effect = send_second
    second_ws.run_forever.side_effect = run_second
    monkeypatch.setattr(qq_gateway.websocket, "WebSocketApp", build_websocket)
    monkeypatch.setattr(qq_gateway.time, "sleep", lambda _seconds: None)

    stop_event = threading.Event()
    ws_holder = []
    gateway_thread = threading.Thread(
        target=qq_gateway.run_gateway,
        kwargs={
            "app_id": "app-id",
            "app_secret": "secret",
            "config_name": "reconnect-test",
            "get_token_fn": lambda _app_id, _secret: "token",
            "get_gateway_url_fn": lambda _token: "wss://gateway.test",
            "on_message_fn": lambda _payload: None,
            "stop_event": stop_event,
            "ws_holder": ws_holder,
        },
        daemon=True,
    )
    gateway_thread.start()

    try:
        assert first_heartbeat_started.wait(0.5)
        return_first_connection.set()
        assert not second_connection_created.wait(0.05)

        release_first_heartbeat.set()
        assert second_connection_created.wait(0.5)
        assert second_heartbeat_started.wait(0.5)

        first_callbacks["on_close"](first_ws, 1000, "stale close")
        release_second_heartbeat.set()
        assert second_followup_started.wait(0.5)

        stop_event.set()
        second_callbacks["on_close"](second_ws, 1000, "current close")
        return_second_connection.set()
        gateway_thread.join(timeout=1.0)
        assert not gateway_thread.is_alive()
    finally:
        stop_event.set()
        release_first_heartbeat.set()
        release_second_heartbeat.set()
        return_first_connection.set()
        return_second_connection.set()
        gateway_thread.join(timeout=1.0)


def test_wechat_bot_stop_reports_each_live_thread_until_retry() -> None:
    """企业微信网关或心跳任一存活时都必须返回未收敛。"""
    client = WeChatBot.__new__(WeChatBot)
    client._stop_event = threading.Event()
    client._authenticated = threading.Event()
    client._ws_app = None
    ws_thread = Mock()
    heartbeat_thread = Mock()
    ws_thread.is_alive.return_value = True
    heartbeat_thread.is_alive.return_value = True
    client._ws_thread = ws_thread
    client._heartbeat_thread = heartbeat_thread
    client._gateway_join_timeout_seconds = 0.01
    client._heartbeat_join_timeout_seconds = 0.01

    assert client.stop() is False
    assert client._ws_thread is ws_thread
    assert client._heartbeat_thread is heartbeat_thread

    ws_thread.is_alive.return_value = False
    heartbeat_thread.is_alive.return_value = False
    assert client.stop() is True


def test_wechat_clawbot_stop_keeps_poll_owner_until_retry() -> None:
    """ClawBot 轮询超时后必须保留线程句柄供后续关闭重试。"""
    client = WechatClawBot.__new__(WechatClawBot)
    client._stop_event = threading.Event()
    poll_thread = Mock()
    poll_thread.is_alive.return_value = True
    client._poll_thread = poll_thread
    client._poll_join_timeout_seconds = 0.01

    assert client.stop() is False
    assert client._poll_thread is poll_thread

    poll_thread.is_alive.return_value = False
    assert client.stop() is True
    assert client._poll_thread is None


def test_feishu_stop_reports_live_ws_thread_until_retry() -> None:
    """飞书 SDK 清理后线程仍存活时不得报告关闭完成。"""
    client = Feishu.__new__(Feishu)
    client._stop_event = threading.Event()
    client._ready = threading.Event()
    client._ws_client = None
    client._ws_loop = None
    ws_thread = Mock()
    ws_thread.is_alive.return_value = True
    client._ws_thread = ws_thread
    client._ws_join_timeout_seconds = 0.01

    assert client.stop() is False
    assert client._ws_thread is ws_thread

    ws_thread.is_alive.return_value = False
    assert client.stop() is True


def test_discord_stop_reports_live_event_loop_thread_until_retry() -> None:
    """Discord 强制停止循环后线程仍存活时必须返回未收敛。"""
    client = Discord.__new__(Discord)
    client._client = Mock()
    client._loop = Mock()
    client._loop.is_running.return_value = False
    event_loop_thread = Mock()
    event_loop_thread.is_alive.return_value = True
    client._thread = event_loop_thread
    client._stop_requested = threading.Event()
    client._ready_event = threading.Event()
    client._thread_join_timeout_seconds = 0.01

    assert client.stop() is False
    assert client._thread is event_loop_thread

    event_loop_thread.is_alive.return_value = False
    assert client.stop() is True


def test_slack_stop_propagates_socket_close_failure() -> None:
    """Slack Socket Mode close 失败必须保留给 Runtime 重试。"""
    client = Slack.__new__(Slack)
    service = Mock()
    service.close.side_effect = RuntimeError("close failed")
    client._service = service

    assert client.stop() is False
    assert client._service is service

    service.close.side_effect = None
    assert client.stop() is True
