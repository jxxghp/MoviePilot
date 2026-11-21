"""
站点主流程按适配器能力分发，不依赖站点名称、域名或固定协议特判。
"""

import ast
import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.modules.indexer as indexer_module
from app.application.site.contract import SiteSnapshot
from app.application.site.observation import capture_site_search_observation
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.domain.context import Context, TorrentInfo
from app.modules.indexer import IndexerModule
from app.modules.subtitle import SubtitleModule
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.rate import RequestRateLimiter
from app.schemas.exception import RateLimitExceededException
from app.schemas.site import Site


class _CapabilitySpider:
    """
    以任意注册名称提供连接检测、下载配方恢复和换票响应解析的离线适配器。
    """

    def __init__(self, indexer: dict[str, Any]) -> None:
        """
        保存本次站点配置，不访问外部服务。
        """
        self.indexer = indexer

    def test_connection(self) -> tuple[bool, str]:
        """
        返回测试设置的检测结果，保留失败与不支持能力的区别。
        """
        return self.indexer.get("test_result", (True, "适配器连接正常"))

    def get_download_url(self, torrent_id: str) -> str:
        """
        按稳定种子 ID 和当前凭据构造下载地址，不发起换票请求。
        """
        return f"https://download.example/{torrent_id}?key={self.indexer['apikey']}"

    @staticmethod
    def parse_download_response(status_code: Optional[int], payload: Any, request_params: dict[str, Any]) -> str:
        """
        解释测试协议自己的响应，错误必须传播到下载主流程。
        """
        if status_code != 200:
            raise ValueError("适配器换票失败")
        return str(payload[request_params["result"]])


@pytest.fixture
def capability(monkeypatch):
    """
    使用非内置协议名登记适配器，并在用例结束时恢复注册表。
    """
    monkeypatch.setitem(indexer_module.SPIDER_PARSER_CLASSES, "CustomProtocol", _CapabilitySpider)
    return {"id": 42, "name": "任意显示名称", "domain": "https://mirror.example/",
            "parser": "CustomProtocol", "apikey": "current-synthetic-key"}


def test_checkpoint_restoration_uses_registered_download_capability(capability):
    """
    改名的协议仍可恢复配方，恢复过程不修改检查点或使用旧凭据。
    """
    record = {"site": 42, "title": "Series", "page_url": "https://mirror.example/detail/123"}
    restored = IndexerModule.restore_search_torrent(capability, record)
    assert restored is not None
    assert restored.enclosure == "https://download.example/123?key=current-synthetic-key"
    assert "enclosure" not in record


@pytest.mark.parametrize("result", [(True, "适配器连接正常"), (False, "适配器认证失败")])
@pytest.mark.parametrize("resource_key", ["schema", "parser"])
def test_connection_uses_adapter_capability_and_current_site_config(monkeypatch, capability, result, resource_key):
    """
    连接检测按声明能力执行，失败不回退网页，当前配置优先于目录中的旧凭据。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=42, name="更改后的名称", url="https://mirror.example/", domain="mirror.example",
                        apikey="current-synthetic-key", render=1, note={"frozen": {"setting": True}})
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    resource = {resource_key: capability["parser"], "apikey": "stale-synthetic-key", "test_result": result}
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value=resource))))
    module = object.__new__(IndexerModule)
    calls = []

    def dispatch(method, **kwargs):
        """
        保留真实索引模块能力分发，只替换模块管理器边界。
        """
        calls.append((method, kwargs))
        kwargs.pop("raise_exception", None)
        return getattr(module, method)(**kwargs)

    monkeypatch.setattr(chain, "run_module", dispatch)
    generic = Mock(side_effect=AssertionError("已处理的检测结果不能回退网页"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic)
    assert chain.test(site.url) == result
    assert calls[0][0] == "test_site"
    assert calls[0][1]["site"]["apikey"] == site.apikey
    generic.assert_not_called()
    (chain.site_repository.success if result[0] else chain.site_repository.fail).assert_called_once()


def test_missing_connection_module_does_not_fall_back_to_successful_page(monkeypatch):
    """
    模块没有回执不等于适配器不支持，网页成功不能掩盖模块不可用。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=42, name="专站", domain="site.example", url="https://site.example/")
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={}))))
    monkeypatch.setattr(chain, "run_module", Mock(return_value=None))
    generic = Mock(return_value=(True, "连接成功"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic)
    assert chain.test(site.url) == (False, "站点 Module 未提供连接检测结果")
    generic.assert_not_called()
    chain.site_repository.fail.assert_called_once_with("site.example")


def test_registered_subtitle_capability_suppresses_page_and_uses_current_credentials(monkeypatch, capability):
    """
    任意注册的字幕 API 都阻止普通模块探测网页，并接收当前用户而不是资源包的旧凭据。
    """
    calls = []

    def get_links(spider, page_url):
        """
        记录当前认证和详情地址，替代专站字幕网络请求。
        """
        calls.append((spider.indexer, page_url))
        return ["https://subtitle.example/download"]

    monkeypatch.setattr(_CapabilitySpider, "get_subtitle_links", get_links, raising=False)
    site = Site(id=42, domain="mirror.example", url="https://mirror.example/", apikey="current-synthetic-key")
    query = Mock(get_sync=Mock(return_value=site))
    for name in ("app.modules.indexer", "app.modules.subtitle"):
        monkeypatch.setattr(f"{name}.get_configured_site_query_service", Mock(return_value=query))
        monkeypatch.setattr(f"{name}.SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value=capability))))
    request = Mock(side_effect=AssertionError("已支持的字幕 API 不能访问网页"))
    monkeypatch.setattr("app.modules.subtitle.RequestUtils", request)
    context = Context(torrent_info=TorrentInfo(site=42, page_url="https://mirror.example/detail/123"))
    assert object.__new__(SubtitleModule).site_subtitle_links(context) is None
    assert object.__new__(IndexerModule).site_subtitle_links(context) == ["https://subtitle.example/download"]
    assert calls[0][0]["apikey"] == site.apikey
    assert calls[0][1] == context.torrent_info.page_url
    request.assert_not_called()


@pytest.mark.parametrize("status", [None, 200, 403])
def test_ticket_response_uses_registered_protocol_capability(capability, status):
    """
    换票响应交给任意注册适配器解析，包含无响应及 HTTP 错误而不是按真假值丢弃。
    """
    chain = object.__new__(DownloadChain)
    module = object.__new__(IndexerModule)
    plugins = Mock(get_plugin_modules=Mock(return_value={}))
    chain.eventmanager = Mock()
    chain.messagehelper = Mock()
    chain._module_dispatcher = ModuleInvocationDispatcher(
        module_catalog=Mock(get_running_modules=Mock(return_value=[module])),
        plugin_catalog=plugins,
        plugin_error_handler=chain._ChainBase__handle_plugin_error,
        system_error_handler=chain._ChainBase__handle_system_error,
        rate_limit_handler=chain._ChainBase__handle_rate_limit_error,
    )
    response = None
    if status is not None:
        response = Response()
        response.status_code = status
        response._content = json.dumps({"link": "https://download.example/123"}).encode()
        response.encoding = "utf-8"
    recipe = {"protocol": capability["parser"], "result": "link"}
    if status == 200:
        assert chain._indirect_download_response(response, recipe, {"id": 42}) == "https://download.example/123"
    else:
        with pytest.raises(ValueError, match="适配器换票失败"):
            chain._indirect_download_response(response, recipe, {"id": 42})
    plugins.get_plugin_modules.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()
    chain.messagehelper.put.assert_not_called()


def test_unknown_ticket_response_capability_fails_closed():
    """
    声明了无法解析的协议时不得把响应中的地址当作成功结果。
    """
    module = object.__new__(IndexerModule)
    with pytest.raises(ValueError, match="解析器"):
        module.parse_torrent_download_response(request_params={"protocol": "NotRegistered"}, status_code=200,
                                              payload={"data": {"url": "https://download.example/123"}})


@pytest.mark.parametrize("domain", ["piggo.me", "mirror.example"])
@pytest.mark.parametrize("interval", [0, 1, 6])
def test_shared_request_interval_depends_only_on_site_config(monkeypatch, domain, interval):
    """
    通用流控不按域名强制增加间隔，同样的配置在任何站点产生同样的等待。
    """
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    clock = {"now": 100.0}
    waits = []

    def advance(delay):
        """
        记录配置产生的等待并推进单调时钟。
        """
        waits.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(throttle.time, "sleep", advance)
    site = {"id": 42, "domain": domain, "limit_seconds": interval}
    throttle.wait_for_site_request(site)
    throttle.wait_for_site_request(site)
    assert waits == ([interval] if interval else [])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_search_throttle_is_owned_by_indexer_not_site_name(monkeypatch, capability, asynchronous):
    """
    普通适配器在索引模块申请额度，多请求适配器可自行管理额度，无需判断协议名。
    """
    module = object.__new__(IndexerModule)
    calls = []
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda *_args: True))
    monkeypatch.setattr(indexer_module, "wait_for_site_request", lambda site: calls.append(("sync", site)))
    monkeypatch.setattr(indexer_module, "async_wait_for_site_request", AsyncMock(side_effect=lambda site: calls.append(("async", site))))
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", Mock())
    monkeypatch.setattr(IndexerModule, "_IndexerModule__async_indexer_statistic", AsyncMock())
    monkeypatch.setitem(indexer_module._SPECIALIZED_SEARCH_ARGUMENTS, capability["parser"], ("keyword",))
    monkeypatch.setattr(_CapabilitySpider, "search", lambda *_args, **_kwargs: (False, []), raising=False)
    monkeypatch.setattr(_CapabilitySpider, "async_search", AsyncMock(return_value=(False, [])), raising=False)

    for manages_throttle in (False, True):
        monkeypatch.setattr(_CapabilitySpider, "manages_request_throttle", manages_throttle, raising=False)
        if asynchronous:
            assert await module.async_search_torrents(capability, "Series") == []
        else:
            assert module.search_torrents(capability, "Series") == []
    assert calls == [("async" if asynchronous else "sync", capability)]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_unissued_search_is_deferred_without_failure_statistics(monkeypatch, capability, asynchronous):
    """
    索引模块等待期间遇到远端冷却时不发出请求、不记一次站点失败，并保留原始原因。
    """
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda *_args: True))
    error = RateLimitExceededException("站点限流：请等待 600 秒")
    monkeypatch.setattr(indexer_module, "wait_for_site_request", Mock(side_effect=error))
    monkeypatch.setattr(indexer_module, "async_wait_for_site_request", AsyncMock(side_effect=error))
    statistic = Mock()
    async_statistic = AsyncMock()
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", statistic)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__async_indexer_statistic", async_statistic)
    monkeypatch.setitem(indexer_module._SPECIALIZED_SEARCH_ARGUMENTS, capability["parser"], ("keyword",))
    with capture_site_search_observation() as observation:
        result = (await module.async_search_torrents(capability, "Series") if asynchronous else
                  module.search_torrents(capability, "Series"))
    assert result == []
    assert not observation.attempted and observation.outcome == "deferred"
    assert observation.error == str(error)
    statistic.assert_not_called()
    async_statistic.assert_not_called()


@pytest.mark.parametrize("relative", [
    "app/application/site/throttle.py", "app/domain/site.py", "app/chain/site.py",
    "app/chain/search/provider.py", "app/chain/download/submission.py", "app/modules/indexer/__init__.py",
])
@pytest.mark.parametrize("site_name", ["piggo", "hdarea"])
def test_main_workflows_do_not_branch_on_site_name(relative, site_name):
    """
    主流程不按站点名分支，连通性与适配器的键值注册属于正常协议声明。
    """
    source = Path(__file__).resolve().parents[1] / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    references = []
    for branch in ast.walk(tree):
        if isinstance(branch, ast.If):
            conditions = [branch.test]
        elif isinstance(branch, ast.Match):
            conditions = [branch.subject, *(case.pattern for case in branch.cases)]
        else:
            continue
        for condition in conditions:
            for node in ast.walk(condition):
                value = node.value if isinstance(node, ast.Constant) else getattr(node, "id", getattr(node, "attr", ""))
                if isinstance(value, str) and site_name in value.casefold():
                    references.append((node.lineno, value))
    assert not references
