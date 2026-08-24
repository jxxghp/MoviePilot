import asyncio
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
