"""验证内置网络探针与对应模块请求契约保持一致。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
