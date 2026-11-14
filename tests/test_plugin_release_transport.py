"""
验证插件 Release 下载出口、凭据隔离和安装方式配置。
"""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.adapters.external.plugin import client
from app.adapters.external.plugin.client import PluginPackageSourceClient
from app.adapters.system.plugin import package
from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.config import ConfigModel


def _archive() -> bytes:
    """
    生成可安装的插件 ZIP，不访问网络或运行目录。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__init__.py", "plugin_payload = True\n")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mirror_result", ["success", "timeout", "http_error", "html"])
@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_release_install_uses_anonymous_mirror_then_authenticated_api(
    monkeypatch, tmp_path, asynchronous, mirror_result, use_proxy, authenticated,
):
    """
    镜像只收到公开下载地址与无凭据请求头，失败后由原 API 出口接管。
    """
    headers = {"Authorization": "Bearer test-token", "Cookie": "test-cookie"} if authenticated else {}
    proxy = {"https": "http://proxy.example:7890"}
    settings = {
        "GITHUB_PROXY": "https://mirror.example",
        "PROXY_HOST": "http://proxy.example:7890" if use_proxy else None,
        "PROXY": proxy if use_proxy else None,
        "PLUGIN_RELEASE_TIMEOUT": 7,
        "REPO_GITHUB_HEADERS": lambda **_kwargs: headers.copy(),
    }
    for module in (client, package):
        monkeypatch.setattr(
            module, "get_runtime_setting", lambda key, default=None: settings.get(key, default),
        )
    requests = []
    archive = _archive()

    def send(options, *, url, raise_exception):
        """
        回放元数据、镜像故障及 API 二进制下载，并记录真实出口参数。
        """
        assert raise_exception is True
        requests.append((url, options))
        if "/releases/tags/" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"assets": [{"name": "demoplugin_v1.2.3.zip", "id": 123}]},
            )
        if url.startswith("https://mirror.example/"):
            if mirror_result == "timeout":
                raise TimeoutError("mirror unavailable")
            return SimpleNamespace(
                status_code=503 if mirror_result == "http_error" else 200,
                content=b"<html>download unavailable</html>" if mirror_result == "html" else archive,
            )
        return SimpleNamespace(status_code=200, content=archive)

    def request_factory(**options):
        """
        为同步或异步适配器提供相同的离线响应序列。
        """
        request_mock = AsyncMock if asynchronous else Mock
        return SimpleNamespace(
            get_res=request_mock(side_effect=lambda **kwargs: send(options, **kwargs)),
        )

    monkeypatch.setattr(client, "AsyncRequestUtils" if asynchronous else "RequestUtils", request_factory)
    manager = PluginPackageManager(source=PluginPackageSourceClient(), plugin_root=tmp_path)
    if asynchronous:
        result = await manager.async_install_from_release("DemoPlugin", "owner/repo", "DemoPlugin_v1.2.3")
    else:
        result = manager.install_from_release("DemoPlugin", "owner/repo", "DemoPlugin_v1.2.3")

    assert result == (True, "")
    assert (tmp_path / "demoplugin" / "__init__.py").read_text() == "plugin_payload = True\n"
    assert requests[0] == (
        "https://api.github.com/repos/owner/repo/releases/tags/DemoPlugin_v1.2.3",
        {"headers": headers, "timeout": 7, **({"proxies": proxy} if use_proxy else {})},
    )
    assert requests[1] == (
        "https://mirror.example/https://github.com/owner/repo/releases/download/"
        "DemoPlugin_v1.2.3/demoplugin_v1.2.3.zip",
        {"headers": {"Accept": "application/octet-stream"}, "timeout": 7},
    )
    if mirror_result == "success":
        assert len(requests) == 2
    else:
        assert requests[2:] == [(
            "https://api.github.com/repos/owner/repo/releases/assets/123",
            {
                "headers": {**headers, "Accept": "application/octet-stream"},
                "timeout": 7,
                **({"proxies": proxy} if use_proxy else {}),
            },
        )]
    assert headers == ({"Authorization": "Bearer test-token", "Cookie": "test-cookie"} if authenticated else {})


@pytest.mark.parametrize("use_proxy", [False, True])
def test_unconfigured_mirror_preserves_authenticated_asset_api(monkeypatch, use_proxy):
    """
    未配置镜像时，带令牌的资产 API 只使用原有代理或直连出口。
    """
    proxy = {"https": "http://proxy.example:7890"}
    settings = {"GITHUB_PROXY": "", "PROXY_HOST": "proxy" if use_proxy else None, "PROXY": proxy}
    monkeypatch.setattr(client, "get_runtime_setting", lambda key: settings[key])
    headers = {"authorization": "Bearer private-token", "Accept": "application/octet-stream"}
    api_url = "https://api.github.com/repos/owner/private/releases/assets/123"
    strategies = client.PluginMarketTransport._build_github_request_strategies(
        api_url, headers=headers, timeout=7, is_api=True,
        mirror_url="https://github.com/owner/private/releases/download/v1/plugin.zip",
    )
    assert strategies == [(
        "代理" if use_proxy else "直连", api_url,
        {"headers": headers, "timeout": 7, **({"proxies": proxy} if use_proxy else {})},
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_filelist_preference_skips_release_requests(monkeypatch, tmp_path, asynchronous):
    """
    关闭默认 Release 安装后，同步与异步入口都直接准备索引文件。
    """
    settings = {"PLUGIN_INSTALL_FROM_RELEASE": False, "VERSION_FLAG": "v3"}
    monkeypatch.setattr(package, "get_runtime_setting", lambda key: settings[key])
    source = Mock()
    source.is_local_repo_url.return_value = False
    source.get_repo_info.return_value = ("owner", "repo")
    source.get_plugin_package_version.return_value = "v3"
    metadata = {"DemoPlugin": {"release": True, "version": "1.2.3"}}
    source.get_plugins.return_value = metadata
    source.async_get_plugin_package_version = AsyncMock(return_value="v3")
    source.async_get_plugins = AsyncMock(return_value=metadata)
    manager = PluginPackageManager(source=source, plugin_root=tmp_path)
    prepare = AsyncMock(return_value=(True, "")) if asynchronous else Mock(return_value=(True, ""))
    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_async" if asynchronous
        else "_PluginPackageManager__prepare_content_via_filelist_sync", prepare,
    )

    async def async_flow(_pid, _force, prepare_content, *_args):
        """
        执行异步内容准备，保留本用例关注的安装分支选择。
        """
        return await prepare_content(tmp_path)

    monkeypatch.setattr(
        manager, "_PluginPackageManager__install_flow_async" if asynchronous
        else "_PluginPackageManager__install_flow_sync",
        async_flow if asynchronous else lambda _pid, _force, prepare_content, *_args: prepare_content(tmp_path),
    )
    if asynchronous:
        result = await manager.async_install_raw("DemoPlugin", "https://github.com/owner/repo")
        prepare.assert_awaited_once()
    else:
        result = manager.install_raw("DemoPlugin", "https://github.com/owner/repo")
        prepare.assert_called_once()
    assert result == (True, "")
    source.request.assert_not_called()
    source.async_request.assert_not_called()
    source.get_plugin_release_versions.assert_not_called()
    source.async_get_plugin_release_versions.assert_not_called()


@pytest.mark.parametrize("prefer_release", [False, True])
@pytest.mark.parametrize("release_version", [None, "1.2.0"])
def test_filelist_preference_preserves_explicit_release(monkeypatch, prefer_release, release_version):
    """
    文件列表偏好跳过默认 Release，但用户指定版本时仍保持历史版本准入。
    """
    monkeypatch.setattr(
        package, "get_runtime_setting", lambda key: prefer_release if key == "PLUGIN_INSTALL_FROM_RELEASE" else None,
    )
    selection, message = PluginPackageManager._select_remote_install(
        pid="DemoPlugin", user_repo="owner/repo", package_version="v3",
        meta={"release": True, "version": "1.2.3"},
        release_version=release_version, release_items=[{"version": "1.2.0"}],
    )
    assert message == ""
    assert selection is not None
    expected = f"DemoPlugin_v{release_version or '1.2.3'}" if prefer_release or release_version else None
    assert selection.release_tag == expected
    assert selection.fallback_to_filelist is (prefer_release and release_version is None)


def test_release_configuration_defaults_and_bounds():
    """
    默认保持 Release 优先，超时支持字符串环境值并拒绝无效范围。
    """
    defaults = ConfigModel()
    assert defaults.PLUGIN_INSTALL_FROM_RELEASE is True
    assert defaults.PLUGIN_RELEASE_TIMEOUT == 30
    configured = ConfigModel(PLUGIN_INSTALL_FROM_RELEASE="false", PLUGIN_RELEASE_TIMEOUT="7")
    assert configured.PLUGIN_INSTALL_FROM_RELEASE is False
    assert configured.PLUGIN_RELEASE_TIMEOUT == 7
    for value in (0, -1, 301):
        with pytest.raises(ValidationError):
            ConfigModel(PLUGIN_RELEASE_TIMEOUT=value)


def test_release_configuration_is_registered_in_system_settings():
    """
    新配置须同时进入系统设置目录，避免 API 在加载配置合同时失败。
    """
    from app.application.settings.contract import CORE_SETTING_SPECS, build_value_schema

    preference = CORE_SETTING_SPECS["PLUGIN_INSTALL_FROM_RELEASE"]
    timeout = CORE_SETTING_SPECS["PLUGIN_RELEASE_TIMEOUT"]
    assert preference.group == timeout.group == "plugins"
    assert preference.examples == (True,)
    assert timeout.examples == (30,)
    assert timeout.unit == "seconds"
    assert build_value_schema(preference)["type"] == "boolean"
    assert build_value_schema(timeout)["minimum"] == 1
    assert build_value_schema(timeout)["maximum"] == 300
