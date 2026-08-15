import importlib

from app.sdk.cache import Cache, cached
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaBase, MetaInfo, MetaMusic, NfoReader
from app.sdk.network import RequestUtils, RssHelper, SitesHelper
from app.sdk.plugins import ModuleManager, PluginManager
from app.sdk.services import NotificationHelper
from app.sdk.utilities import StringUtils as UtilityStringUtils
from app.sdk.utilities import decrypt, encrypt


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
    assert ModuleManager is CanonicalModuleManager
    assert PluginManager is CanonicalPluginManager


def test_legacy_common_crypto_aliases_round_trip():
    """v1 插件使用的 common 加解密符号应保持 CryptoJS 密文兼容。"""
    legacy_common = importlib.import_module("app.utils.common")
    legacy_decrypt = legacy_common.decrypt
    legacy_encrypt = legacy_common.encrypt

    message = b"moviepilot-plugin-compat"
    passphrase = b"0123456789abcdef"

    assert legacy_decrypt(legacy_encrypt(message, passphrase), passphrase) == message
