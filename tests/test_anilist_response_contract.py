"""
AniList 的无效 GraphQL 响应不得阻止官方回退与中文合并。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.anilist.anilist import AniListApi


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("proxy_payload", [
    None, [], [1], "invalid", 7, False,
    {"data": []}, {"data": [1]}, {"data": "invalid"}, {"data": 7},
    {"data": False}, {"errors": ["unavailable"], "data": {"Media": {"id": 1}}},
])
def test_invalid_proxy_payload_preserves_official_fallback(asynchronous, proxy_payload):
    """
    无效对象、无效 data 和 GraphQL 错误都应触发既有官方查询路径。
    """
    proxy = Mock(status_code=200)
    proxy.json.return_value = proxy_payload
    official = Mock(status_code=200)
    media = {"id": 12, "title": {"native": "Original"}, "synonyms": ["Alias"]}
    data = {"Media": media}
    official.json.return_value = {"data": data}
    request = AsyncMock(side_effect=[proxy, official]) if asynchronous else Mock(side_effect=[proxy, official])
    translations = [{"id": 12, "title": "中文标题", "synonyms": ["Alias", "Chinese Alias"]}]
    client = AniListApi()
    transport = Mock()
    transport.post_res = request
    transport.get_json = AsyncMock(return_value=translations) if asynchronous else Mock(return_value=translations)
    if asynchronous:
        client._async_request = transport
        result = asyncio.run(client._async_invoke("query", {"id": 12}))
    else:
        client._request = transport
        result = client._invoke("query", {"id": 12})
    assert result is data
    assert media["title"]["chinese"] == "中文标题"
    assert media["synonyms"] == ["Alias", "Chinese Alias"]
    assert client._proxy_available is False
    assert [call.args[0] for call in request.call_args_list] == [client._base_url, client._official_url]
    assert all(call.kwargs["json"] == {"query": "query", "variables": {"id": 12}} for call in request.call_args_list)


@pytest.mark.parametrize("payload", [{}, {"Media": None}, {"Page": {"media": []}}])
def test_valid_graphql_data_preserves_identity(payload):
    """
    有效 data 对象包括空对象均应原样返回，保留既有空结果处理规则。
    """
    response = Mock(status_code=200)
    response.json.return_value = {"data": payload}
    assert AniListApi._extract_response(response) is payload


def test_translation_map_preserves_raw_ids_and_in_place_merge():
    """
    数字和字符串 ID 不互相转换，递归合并保留原对象和别名顺序。
    """
    translated = [
        {"id": 12, "title": "数字", "synonyms": ["A", "B"]},
        {"id": "12", "title": "字符串", "synonyms": []},
        {"id": 0, "title": "ignored"},
    ]
    mapping = AniListApi._build_translation_map(translated)
    number = {"id": 12, "title": {}, "synonyms": ["A"]}
    string = {"id": "12", "title": {}, "synonyms": []}
    payload = {"Page": {"media": [number, string, None, False]}}
    assert AniListApi._inject_chinese(payload, mapping) is payload
    assert number["title"]["chinese"] == "数字"
    assert string["title"]["chinese"] == "字符串"
    assert number["synonyms"] == ["A", "B"]
    assert 0 not in mapping
