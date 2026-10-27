"""
验证插件安装前文档的元数据、所选来源、代际与有界网络探测。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external.plugin import client as client_module
from app.adapters.external.plugin.client import PluginMarketTransport
from app.api.endpoints import plugin as plugin_endpoint
from app.application.plugin.catalog import PluginCatalogQuery, PluginCatalogService
from app.domain.plugin import normalize_plugin_documentation_url
from app.foundation.version import compare_version
from app.runtime.extensions.plugin.metadata import PluginMetadataMapper
from app.schemas.plugin import Plugin

REPO = "https://github.com/example/plugins"
DOC = "https://docs.example.com/plugins/demo#setup"


def _mapper() -> PluginMetadataMapper:
    """
    构造无需插件实例、权限服务或外网的真实元数据映射器。
    """
    return PluginMetadataMapper(
        plugin_instance=lambda _pid: None,
        plugin_class=lambda _pid: None,
        annotate_system_version=lambda info: info,
        annotate_runtime_compatibility=lambda info: info,
        is_package_compatible=lambda _info, _version: True,
        auth_checker=lambda _plugin, _info: True,
        version_compare=lambda *_args: False,
        log=Mock(),
    )


def _candidate(repo: str = REPO, generation: Optional[str] = "v3", **fields) -> Plugin:
    """
    构造尚未安装、带真实包代际的目录候选。
    """
    plugin = Plugin(id="Demo", repo_url=repo, **fields)
    plugin.package_version = generation
    return plugin


def _query(candidates: list[Plugin], readme: AsyncMock) -> PluginCatalogQuery:
    """
    通过窄端口隔离目录与 README 传输，保留真实来源选择逻辑。
    """
    catalog = PluginCatalogService(
        market_loader=Mock(),
        async_market_loader=AsyncMock(),
        installed_plugins_provider=lambda: [],
        plugin_mapper=Mock(),
        is_local_repo=lambda repo: repo.startswith("local://"),
        version_compare=lambda left, operator, right: compare_version(left, operator, right) is True,
        warning=Mock(),
        error=Mock(),
    )
    return PluginCatalogQuery(
        installed_plugins=lambda: [],
        local_plugins=lambda: [],
        local_repo_plugins=lambda: [item for item in candidates if item.is_local],
        online_candidates=AsyncMock(return_value=candidates),
        process_plugins=lambda higher, base: catalog.merge(higher, base, [item.repo_url for item in candidates]),
        identities=AsyncMock(return_value=[]),
        free_threaded=lambda: False,
        readme_url=readme,
    )


@pytest.mark.parametrize("field", ["plugin_readme", "homepage", "project_url"])
def test_market_documentation_aliases_reach_uninstalled_plugin(field: str) -> None:
    """
    市场投影应把文档字段及兼容别名传给未安装插件的公开响应。
    """
    plugin = _mapper().map("Demo", {field: DOC}, REPO, [], 1, "v3")
    assert plugin is not None and plugin.installed is False
    assert plugin.model_dump()["plugin_readme"] == DOC


def test_explicit_documentation_wins_and_invalid_alias_is_skipped() -> None:
    """
    显式字段优先，非法值不会遮住有效兼容字段或使插件消失。
    """
    info = {"plugin_readme": DOC, "homepage": "https://other.example.com"}
    assert _mapper().map("Demo", info, REPO, [], 1).plugin_readme == DOC
    info["plugin_readme"] = "javascript:alert(1)"
    assert _mapper().map("Demo", info, REPO, [], 1).plugin_readme == info["homepage"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        {},
        "",
        "javascript:alert(1)",
        "//example.com",
        "https://a b.com",
        "https://user:pass@example.com",
        "https://[",
        "https://example.com/" + "x" * 2048,
    ],
)
def test_invalid_documentation_is_ignored(value: object) -> None:
    """
    非 HTTP(S)、非法类型与超长链接不能成为可点击的文档入口。
    """
    assert normalize_plugin_documentation_url(value) is None
    assert Plugin(id="Demo", plugin_readme=value).plugin_readme is None


@pytest.mark.parametrize("value", [123, "x" * 2049])
def test_index_sanitizes_documentation_without_dropping_plugin(value: object) -> None:
    """
    索引传输应丢弃不合法文档字段，继续保留可安装的插件条目。
    """
    entry, error, dropped = PluginMarketTransport._PluginMarketTransport__normalize_plugin_index_entry(
        "Demo",
        {"version": "1.0.0", "plugin_readme": value},
    )
    assert error is None and entry == {"version": "1.0.0"}
    assert dropped == ["plugin_readme"]


@pytest.mark.asyncio
async def test_documentation_uses_selected_source_and_its_generation() -> None:
    """
    同名插件必须使用所选仓库的真实 V2 代际，不能借用另一仓库的显式说明。
    """
    readme = AsyncMock(return_value=DOC)
    query = _query(
        [
            _candidate(plugin_readme="https://other.example.com"),
            _candidate("https://github.com/another/plugins", "v2"),
        ],
        readme,
    )
    result = await query.documentation("demo", "https://github.com/another/plugins")
    assert result.url == DOC
    readme.assert_awaited_once_with("Demo", "https://github.com/another/plugins", "v2")


@pytest.mark.asyncio
async def test_declared_documentation_avoids_readme_request() -> None:
    """
    声明外链时不请求外部文档正文，也不探测仓库 README。
    """
    readme = AsyncMock()
    assert (await _query([_candidate(plugin_readme=DOC)], readme).documentation("Demo", REPO)).url == DOC
    readme.assert_not_awaited()


@pytest.mark.asyncio
async def test_documentation_matches_market_choice_across_generations() -> None:
    """
    同仓库 V2 版本较新时，文档沿用真实市场候选，不能固定选择 V3 目录。
    """
    readme = AsyncMock(return_value=DOC)
    query = _query(
        [
            _candidate(generation="v3", plugin_version="1.0.0"),
            _candidate(generation="v2", plugin_version="2.0.0"),
        ],
        readme,
    )
    assert (await query.documentation("Demo", REPO)).url == DOC
    readme.assert_awaited_once_with("Demo", REPO, "v2")


@pytest.mark.asyncio
async def test_unknown_source_and_local_paths_are_not_probed() -> None:
    """
    未收录来源与本地路径不能触发远端文档或本地文件探测。
    """
    readme = AsyncMock()
    local = "local://Demo?path=/private/plugins&version=v3"
    query = _query([_candidate(), _candidate(local, is_local=True)], readme)
    assert (await query.documentation("Demo", "https://attacker.example.com")).url is None
    assert (await query.documentation("Missing", REPO)).url is None
    assert (await query.documentation("Demo", local)).url is None
    readme.assert_not_awaited()


@pytest.mark.asyncio
async def test_documentation_endpoint_delegates_without_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    说明接口无需已安装状态，按调用方的插件和来源返回结构化入口。
    """
    service = _query([_candidate(plugin_readme=DOC)], AsyncMock())
    monkeypatch.setattr(plugin_endpoint, "get_plugin_catalog_query", lambda: service)
    result = await plugin_endpoint.plugin_readme("Demo", repo_url=REPO, _=None)
    assert result.model_dump() == {"url": DOC}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation,directory", [(None, "plugins"), ("v1", "plugins"), ("v2", "plugins.v2"), ("v3", "plugins.v3")]
)
async def test_readme_probe_uses_proxy_stream_and_caches_result(
    monkeypatch: pytest.MonkeyPatch,
    generation: Optional[str],
    directory: str,
) -> None:
    """
    成功探测复用代理策略、关闭流且不读取正文，相同来源重复打开不再出站。
    """
    calls = []

    @asynccontextmanager
    async def stream(**kwargs):
        """
        提供只有响应头的流，禁止依赖远端文档正文。
        """
        calls.append(kwargs)
        yield SimpleNamespace(status_code=200, headers={"content-type": "text/plain"})

    request = Mock(return_value=SimpleNamespace(get_stream=stream))
    monkeypatch.setattr(client_module, "AsyncRequestUtils", request)
    monkeypatch.setattr(client_module, "get_runtime_setting", lambda _key: lambda **_kwargs: {"X-Test": "header"})
    strategies = Mock(return_value=[("镜像站", "https://proxy.example.com/readme", {"timeout": 5})])
    monkeypatch.setattr(PluginMarketTransport, "_build_github_request_strategies", strategies)
    transport = PluginMarketTransport()
    await transport.async_get_plugin_readme_url.cache_clear()
    try:
        expected = f"{REPO}/blob/main/{directory}/demo/README.md"
        assert await transport.async_get_plugin_readme_url("Demo", REPO, generation) == expected
        assert await transport.async_get_plugin_readme_url("Demo", REPO, generation) == expected
        assert calls == [{"url": "https://proxy.example.com/readme", "raise_exception": True}]
        strategies.assert_called_once_with(
            f"https://raw.githubusercontent.com/example/plugins/main/{directory}/demo/README.md",
            headers={"X-Test": "header"},
            timeout=5,
        )
    finally:
        await transport.async_get_plugin_readme_url.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,content_type", [(404, "text/plain"), (200, "text/html"), (None, "")])
async def test_unavailable_readme_is_negative_cached(
    monkeypatch: pytest.MonkeyPatch,
    status: Optional[int],
    content_type: str,
) -> None:
    """
    README 缺失或代理返回 HTML 时隐藏入口，并缓存失败避免重复探测。
    """
    calls = []

    @asynccontextmanager
    async def stream(**_kwargs):
        """
        回放缺失文件与镜像验证页的响应头。
        """
        calls.append(True)
        yield SimpleNamespace(status_code=status, headers={"content-type": content_type}) if status else None

    monkeypatch.setattr(client_module, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_stream=stream)))
    transport = PluginMarketTransport()
    await transport.async_get_plugin_readme_url.cache_clear()
    try:
        assert await transport.async_get_plugin_readme_url("Demo", REPO, "v3") is None
        assert await transport.async_get_plugin_readme_url("Demo", REPO, "v3") is None
        assert len(calls) == 1
    finally:
        await transport.async_get_plugin_readme_url.cache_clear()


@pytest.mark.asyncio
async def test_readme_probe_has_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    整个代理降级探测受总时限约束，超时被收口为空入口。
    """

    @asynccontextmanager
    async def stream(**_kwargs):
        """
        模拟一直等待响应头的网络边界。
        """
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(client_module, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_stream=stream)))
    monkeypatch.setattr(client_module, "PLUGIN_README_REQUEST_TIMEOUT", 0.01)
    transport = PluginMarketTransport()
    await transport.async_get_plugin_readme_url.cache_clear()
    try:
        assert await asyncio.wait_for(transport.async_get_plugin_readme_url("Demo", REPO, "v3"), timeout=1) is None
    finally:
        await transport.async_get_plugin_readme_url.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin_id,repo,generation", [("../Demo", REPO, "v3"), ("Demo", "file:///private", "v3"), ("Demo", REPO, "../v3")]
)
async def test_readme_probe_rejects_invalid_identity_before_network(
    monkeypatch: pytest.MonkeyPatch,
    plugin_id: str,
    repo: str,
    generation: str,
) -> None:
    """
    非法插件、仓库与代际不能拼入 README 路径或触发网络请求。
    """
    request = Mock()
    monkeypatch.setattr(client_module, "AsyncRequestUtils", request)
    transport = PluginMarketTransport()
    await transport.async_get_plugin_readme_url.cache_clear()
    try:
        assert await transport.async_get_plugin_readme_url(plugin_id, repo, generation) is None
        request.assert_not_called()
    finally:
        await transport.async_get_plugin_readme_url.cache_clear()


@pytest.mark.asyncio
async def test_readme_probe_falls_back_after_proxy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    镜像失败后沿既有出口继续探测，成功时仍返回规范 GitHub 文档地址。
    """
    calls = []

    @asynccontextmanager
    async def stream(**kwargs):
        """
        模拟镜像故障与第二出口正常返回。
        """
        calls.append(kwargs["url"])
        if len(calls) == 1:
            raise OSError("proxy unavailable")
        yield SimpleNamespace(status_code=200, headers={"content-type": "text/plain"})

    monkeypatch.setattr(client_module, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_stream=stream)))
    monkeypatch.setattr(
        PluginMarketTransport,
        "_build_github_request_strategies",
        Mock(
            return_value=[
                ("镜像站", "https://proxy.example.com", {}),
                ("直连", "https://raw.githubusercontent.com/example/plugins/main/plugins.v3/demo/README.md", {}),
            ]
        ),
    )
    transport = PluginMarketTransport()
    await transport.async_get_plugin_readme_url.cache_clear()
    try:
        assert (
            await transport.async_get_plugin_readme_url("Demo", REPO, "v3")
            == f"{REPO}/blob/main/plugins.v3/demo/README.md"
        )
        assert len(calls) == 2
    finally:
        await transport.async_get_plugin_readme_url.cache_clear()
