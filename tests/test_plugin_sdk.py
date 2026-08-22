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
from app.sdk.utilities import decrypt, encrypt


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


def test_agent_tool_base_and_tags_are_the_canonical_objects():
    """智能体工具族的基类与标签必须经 SDK 取得，且与宿主是同一个对象。

    该族的声明契约按 ``impl`` 的继承关系判定，基类因此不是便利品而是声明成立的前提；
    标签同样跑不掉——只读子代理按 ``ToolTag.Read`` 筛选工具，不带该标签的工具选不上。
    SDK 不给这两个出口，写工具的扩展就只能直接 import ``app.agent.tools.base``，而那条
    路径被插件导入边界门禁判为越界。
    """
    from app.agent.tools.base import MoviePilotTool as CanonicalTool
    from app.agent.tools.tags import ToolTag as CanonicalToolTag
    from app.sdk.agent import MoviePilotTool, ToolTag

    assert MoviePilotTool is CanonicalTool
    assert ToolTag is CanonicalToolTag


def test_agent_tool_written_against_the_sdk_passes_the_registration_contract():
    """只用 SDK 基类写出来的工具必须过得了登记期契约校验。"""
    from app.runtime.extensions.contract.declaration import AgentToolDeclaration
    from app.runtime.extensions.admission import agent_tool
    from app.sdk.agent import MoviePilotTool

    class _SdkTool(MoviePilotTool):
        """只依赖 SDK 出口的最小工具。"""

        name: str = "sdk_written_tool"
        description: str = "A tool written against the SDK facade only."

        async def run(self, **kwargs) -> str:
            """返回固定结果。"""
            return "ok"

    original = agent_tool._agent_tool_base
    agent_tool.configure_agent_tool_base(MoviePilotTool)
    try:
        violation = agent_tool.agent_tool_declaration_violation(
            AgentToolDeclaration(
                name="sdk_written_tool",
                description="A tool written against the SDK facade only.",
                impl=_SdkTool,
            )
        )
    finally:
        agent_tool._agent_tool_base = original

    assert violation is None


def test_service_instance_protocols_match_the_host_required_methods():
    """按族导出的协议必须与宿主必填集逐族一致。

    协议是手写的可读形态，必填集是宿主判定登记的那一份。两者各写一份就会漂移，本条
    把它们钉在一起：宿主改必填集而协议没跟上，或协议多写了一个宿主根本不判的方法，
    都在这里红。
    """
    from app.runtime.extensions.contract.service_instance import (
        SERVICE_INSTANCE_REQUIRED_METHODS,
    )
    from app.sdk.service_instances import (
        DownloaderInstance,
        MediaServerInstance,
        NotificationInstance,
    )

    protocols = {
        "downloader": DownloaderInstance,
        "mediaserver": MediaServerInstance,
        "notification": NotificationInstance,
    }

    assert set(protocols) == set(SERVICE_INSTANCE_REQUIRED_METHODS)
    for capability, protocol in protocols.items():
        assert sorted(protocol.__protocol_attrs__) == sorted(
            SERVICE_INSTANCE_REQUIRED_METHODS[capability]
        ), capability


def test_required_methods_accessor_reads_through_to_the_host_table():
    """必填集访问器直读宿主那张表，不在表里的族答空元组。"""
    from app.runtime.extensions.contract.service_instance import (
        SERVICE_INSTANCE_REQUIRED_METHODS,
    )
    from app.sdk.service_instances import service_instance_required_methods

    for capability, required in SERVICE_INSTANCE_REQUIRED_METHODS.items():
        assert service_instance_required_methods(capability) == tuple(required)
    assert service_instance_required_methods("storage") == ()
    assert service_instance_required_methods("nonexistent-family") == ()


def test_declaring_the_protocol_stays_optional_for_service_instances():
    """协议可继承可不继承：形状判定按方法名走，不按 MRO 走。

    把鸭子类型改成强制继承会让走 ``factory`` 路径与既有的实现当场失效，因此协议只是
    写代码时的可读形态。本条同时压住两个方向：继承了的过，没继承但方法齐的也过。
    """
    from app.runtime.extensions.contract.service_instance import (
        service_instance_shape_violation,
    )
    from app.sdk.service_instances import DownloaderInstance

    class _Inherited(DownloaderInstance):
        """显式继承协议的实现。"""

        def is_inactive(self) -> bool:
            """返回固定结果。"""
            return False

        def reconnect(self) -> None:
            """不做任何事。"""

    class _DuckTyped:
        """不继承任何协议、只把方法写齐的实现。"""

        def is_inactive(self) -> bool:
            """返回固定结果。"""
            return False

        def reconnect(self) -> None:
            """不做任何事。"""

    class _Missing:
        """漏掉 reconnect 的实现。"""

        def is_inactive(self) -> bool:
            """返回固定结果。"""
            return False

    assert service_instance_shape_violation("downloader", _Inherited) is None
    assert service_instance_shape_violation("downloader", _DuckTyped) is None
    assert "reconnect" in service_instance_shape_violation("downloader", _Missing)


def test_service_capabilities_reads_through_to_the_family_registry():
    """能力标签出口直读服务族登记表，扩展登记的新族当场可见、注销后随即消失。

    SDK 若抄一份固定清单，这条断言的后半段就永远过不了——而那正是「族是登记出来的」
    这句话的实际含义。
    """
    from app.runtime.extensions.contract.extension import ExtensionDistribution
    from app.runtime.extensions.registry.service_family import service_family_registry
    from app.sdk.service_instances import service_capabilities

    builtin = service_capabilities()

    assert builtin["downloader"] == "下载器"
    assert set(builtin) == set(service_family_registry.capabilities())

    owner = "SdkCapabilityProbe@probe"
    service_family_registry.register(
        "sdkprobe", "探针族", owner=owner, distribution=ExtensionDistribution.MARKET
    )
    try:
        assert service_capabilities()["sdkprobe"] == "探针族"
    finally:
        service_family_registry.unregister_owner(owner)

    assert service_capabilities() == builtin


def test_the_sdk_publishes_no_fixed_capability_constants():
    """SDK 不得再按族散落固定能力标签常量。

    两个族有常量、三个族没有，是同一件事的两种口径；口径统一为「一个都不给，标签直接
    写字面量，现有哪些族问登记表」。本条盯住这条规矩不被逐个族地重新破开。
    """
    from app.sdk import _exports

    offenders = {
        f"{sdk_name}.{name}"
        for sdk_name, symbols in _exports.SDK_DECLARED_EXPORTS.items()
        for name in symbols
        if name.endswith("_CAPABILITY")
    }

    assert offenders == set()
