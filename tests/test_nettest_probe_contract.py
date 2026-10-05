"""验证内置网络探针与对应模块请求契约保持一致。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.network import NetworkTestService


def _network_test_service(transport, **settings) -> NetworkTestService:
    """创建读取独立设置快照的网络探测服务。"""
    values = {"NORMAL_USER_AGENT": "MoviePilot-Test", **settings}
    return NetworkTestService(
        transport=transport,
        settings=lambda key, default=None: values.get(key, default),
        logger=Mock(),
    )


def test_anilist_probe_uses_post_graphql_request() -> None:
    """AniList 官方 GraphQL 探针应使用模块相同的 POST JSON 协议。"""
    captured = {}
    response = SimpleNamespace(
        status_code=200,
        headers={},
        text='{"data":{"Media":{"id":1}}}',
        aclose=AsyncMock(),
    )

    async def respond(method, url, **options):
        """记录 AniList 探测请求并返回成功响应。"""
        captured.update(method=method, url=url, **options)
        return response

    transport = SimpleNamespace(request=AsyncMock(side_effect=respond))
    result = asyncio.run(
        _network_test_service(transport).execute(target_id="anilist_api")
    )

    assert result.success
    assert captured["method"] == "POST"
    assert captured["url"] == "https://graphql.anilist.co"
    assert captured["json_body"] == {"query": "{ Media(id: 1) { id } }"}
    assert captured["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def test_theaudiodb_probe_uses_configured_key_and_module_health_endpoint() -> None:
    """TheAudioDB 探针应使用模块配置的 Key 和相同的艺术家搜索接口。"""
    configured_key = "custom-audio-key"
    captured = {}
    response = SimpleNamespace(
        status_code=200,
        headers={},
        text='{"artists":[]}',
        aclose=AsyncMock(),
    )

    async def respond(method, url, **options):
        """记录 TheAudioDB 探测请求并返回成功响应。"""
        captured.update(method=method, url=url, **options)
        return response

    transport = SimpleNamespace(request=AsyncMock(side_effect=respond))
    service = _network_test_service(
        transport,
        THEAUDIODB_API_KEY=configured_key,
    )

    public_target = next(
        target for target in service.list_targets() if target.id == "theaudiodb_api"
    )
    result = asyncio.run(service.execute(target_id="theaudiodb_api"))

    assert result.success
    assert public_target.address == "https://www.theaudiodb.com"
    assert configured_key not in public_target.address
    assert captured["method"] == "GET"
    assert captured["url"] == (
        "https://www.theaudiodb.com/api/v1/json/"
        "custom-audio-key/search.php?s=coldplay"
    )


@pytest.mark.parametrize("github_headers", [None, {"Authorization": "Bearer github-token"}])
def test_network_proxy_probe_uses_configured_github_identity(github_headers) -> None:
    """通用代理探针应沿用 GitHub API 身份，且公开目录不泄漏认证头。"""
    captured = {}

    async def respond(method, url, **options):
        """按请求是否携带预期身份模拟 GitHub API 响应。"""
        captured.update(method=method, url=url, **options)
        return SimpleNamespace(
            status_code=200 if options["headers"] == github_headers else 403,
            headers={},
            text="{}",
            aclose=AsyncMock(),
        )

    transport = SimpleNamespace(request=AsyncMock(side_effect=respond))
    service = _network_test_service(
        transport,
        PROXY={"https": "http://proxy.example:7890"},
        PROXY_HOST="http://proxy.example:7890",
        GITHUB_HEADERS=github_headers,
    )

    public_target = next(target for target in service.list_targets() if target.id == "network_proxy")
    result = asyncio.run(service.execute(target_id="network_proxy"))

    assert result.success
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.github.com"
    assert captured["proxy"] == {"https": "http://proxy.example:7890"}
    assert captured["headers"] == github_headers
    assert public_target.address == "http://proxy.example:7890"
    assert "github-token" not in public_target.address


def test_tmdb_web_probe_identifies_as_moviepilot() -> None:
    """TMDB 网站探针应使用程序自身 UA，浏览器 UA 会被站点人机校验页拦成 403。"""
    captured = {}

    async def respond(method, url, **options):
        """按请求头里的 UA 模拟 TMDB 网站对非真实浏览器的响应。"""
        captured.update(method=method, url=url, **options)
        identifies_as_app = (options["headers"] or {}).get("User-Agent") == "MoviePilot/3.1.1 (Linux; x86_64)"
        return SimpleNamespace(
            status_code=200 if identifies_as_app else 403,
            headers={},
            text="",
            aclose=AsyncMock(),
        )

    transport = SimpleNamespace(request=AsyncMock(side_effect=respond))
    service = _network_test_service(
        transport,
        USER_AGENT="MoviePilot/3.1.1 (Linux; x86_64)",
    )

    result = asyncio.run(service.execute(target_id="tmdb_web"))

    assert result.success
    assert captured["method"] == "GET"
    assert captured["url"] == "https://www.themoviedb.org"
    assert captured["headers"] == {"User-Agent": "MoviePilot/3.1.1 (Linux; x86_64)"}
