import threading
from unittest.mock import Mock, patch

import pytest

from app.modules import _MessageBase
from app.modules.discord import DiscordModule
from app.modules.feishu import FeishuModule
from app.modules.filter import FilterModule
from app.modules.plex import PlexModule
from app.modules.qqbot import QQBotModule
from app.modules.slack import SlackModule
from app.modules.telegram import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.modules.themoviedb import TheMovieDbModule
from app.modules.trimemedia import TrimeMediaModule
from app.modules.ugreen import UgreenModule
from app.modules.wechat import WechatModule
from app.modules.wechatclawbot import WechatClawBotModule


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
    client._bot = bot
    polling_thread = Mock()
    client._polling_thread = polling_thread

    client.stop()
    client.stop()

    bot.stop_bot.assert_called_once_with()
    polling_thread.join.assert_called_once_with()
    assert client._bot is None
    assert client._polling_thread is None
