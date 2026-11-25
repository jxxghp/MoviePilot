"""
AniList 的无效 GraphQL 响应应返回失败，中文合并保留原始身份与别名顺序。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.anilist.anilist import AniListApi


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("payload", [
    None, [], [1], "invalid", 7, False,
    {"data": []}, {"data": [1]}, {"data": "invalid"}, {"data": 7},
    {"data": False}, {"errors": ["unavailable"], "data": {"Media": {"id": 1}}},
])
def test_invalid_official_payload_does_not_download_translations(asynchronous: bool, payload: object) -> None:
    """
    无效对象、无效 data 和 GraphQL 错误不得触发数据集下载或再次查询。
    """
    response = Mock(status_code=200)
    response.json.return_value = payload
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    client = AniListApi()
    transport = Mock()
    transport.post_res = request
    transport.get_json = AsyncMock() if asynchronous else Mock()
    if asynchronous:
        client._async_request = transport
        result = asyncio.run(client._async_invoke("query", {"id": 12}))
    else:
        client._request = transport
        result = client._invoke("query", {"id": 12})
    assert result is None
    request.assert_called_once_with(client._official_url, json={"query": "query", "variables": {"id": 12}})
    transport.get_json.assert_not_called()


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
