import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.sdk.cache import Cache, cached
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaBase, MetaInfo, MetaMusic, NfoReader
from app.sdk.network import RequestUtils, RssHelper, SitesHelper
from app.sdk.plugins import ModuleManager, PluginManager
from app.sdk.services import NotificationHelper
from app.sdk.utilities import StringUtils as UtilityStringUtils
from app.sdk.utilities import convert, decrypt, encrypt


PROJECT_ROOT = Path(__file__).parents[1]


def test_sdk_exports_canonical_plugin_interfaces():
    """SDK 应复用 canonical 对象，不复制实现或制造第二套单例。"""
    from app.domain.context import MediaInfo as CanonicalMediaInfo
    from app.domain.meta.metabase import MetaBase as CanonicalMetaBase
    from app.domain.meta.metamusic import MetaMusic as CanonicalMetaMusic
    from app.domain.metainfo import MetaInfo as CanonicalMetaInfo
    from app.domain.scraper import NfoReader as CanonicalNfoReader
    LegacyDomainStringUtils = importlib.import_module(
        "app.domain.string"
    ).StringUtils
    from app.foundation.crypto import CryptoJsUtils
    from app.foundation.text import convert as canonical_convert
    from app.runtime.extensions.module_manager import ModuleManager as CanonicalModuleManager
    from app.runtime.extensions.plugin_manager import PluginManager as CanonicalPluginManager
    from app.adapters.network.http import RequestUtils as CanonicalRequestUtils
    from app.application.rss import RssHelper as CanonicalRssHelper
    from app.application.site.sites import SitesHelper as CanonicalSitesHelper  # pylint: disable=no-name-in-module
    from app.runtime.cache import Cache as CanonicalCache
    from app.runtime.cache import cached as canonical_cached
    from app.runtime.config import settings as canonical_settings
    from app.runtime.events import Event as CanonicalEvent
    from app.runtime.events import eventmanager as canonical_eventmanager
    from app.runtime.log import logger as canonical_logger
    from app.application.notification import NotificationHelper as CanonicalNotificationHelper

    assert Cache is CanonicalCache
    assert cached is canonical_cached
    assert settings is canonical_settings
    assert Event is CanonicalEvent
    assert eventmanager is canonical_eventmanager
    assert logger is canonical_logger
    assert MediaInfo is CanonicalMediaInfo
    assert MetaBase is CanonicalMetaBase
    assert MetaInfo is CanonicalMetaInfo
    assert MetaMusic is CanonicalMetaMusic
    assert NfoReader is CanonicalNfoReader
    assert RequestUtils is CanonicalRequestUtils
    assert RssHelper is CanonicalRssHelper
    assert SitesHelper is CanonicalSitesHelper
    assert NotificationHelper is CanonicalNotificationHelper
    assert UtilityStringUtils is LegacyDomainStringUtils
    assert decrypt is CryptoJsUtils.decrypt
    assert encrypt is CryptoJsUtils.encrypt
    assert convert is canonical_convert
    assert ModuleManager is CanonicalModuleManager
    assert PluginManager is CanonicalPluginManager
    assert CanonicalPluginManager.__module__ == "app.runtime.extensions.plugin_manager"


def test_legacy_common_crypto_aliases_round_trip():
    """v1 插件使用的 common 加解密符号应保持 CryptoJS 密文兼容。"""
    legacy_common = importlib.import_module("app.utils.common")
    legacy_decrypt = legacy_common.decrypt
    legacy_encrypt = legacy_common.encrypt

    message = b"moviepilot-plugin-compat"
    passphrase = b"0123456789abcdef"

    assert legacy_decrypt(legacy_encrypt(message, passphrase), passphrase) == message


def test_browser_sdk_import_is_provider_free():
    """仅导入浏览器 SDK 不得加载浏览器或虚拟显示实现。"""
    script = """
import sys
import app.sdk.browser

for name in (
    "cloakbrowser",
    "pyvirtualdisplay",
    "app.adapters.network.browser",
    "app.adapters.system.display.resource",
):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_browser_sdk_delegates_sync_and_async_launch(monkeypatch):
    """SDK 只转发浏览器参数，不复制宿主生命周期实现。"""
    from app.sdk import browser as browser_sdk
    from app.adapters.network import browser as browser_adapter

    sync_context = object()
    async_context = object()
    sync_launch = MagicMock(return_value=sync_context)
    async_launch = AsyncMock(return_value=async_context)
    monkeypatch.setattr(browser_adapter, "launch_browser_context", sync_launch)
    monkeypatch.setattr(browser_adapter, "launch_browser_context_async", async_launch)

    assert browser_sdk.launch_browser_context(headless=False, locale="zh-CN") is sync_context

    async def run_async():
        return await browser_sdk.launch_browser_context_async(
            headless=True,
            timezone="Asia/Shanghai",
        )

    import asyncio

    assert asyncio.run(run_async()) is async_context
    sync_launch.assert_called_once_with(headless=False, locale="zh-CN")
    async_launch.assert_awaited_once_with(
        headless=True,
        timezone="Asia/Shanghai",
    )
