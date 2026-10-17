"""
验证 Jellyfin JSON 边界不会把空路径或标识变成文本。
"""

from unittest.mock import Mock

import pytest

from app.modules.jellyfin.jellyfin import Jellyfin


def _client(monkeypatch: pytest.MonkeyPatch, payload: object) -> Jellyfin:
    """
    注入离线响应，避免初始化期间查询真实服务器。
    """
    client = Jellyfin.__new__(Jellyfin)
    client._host = "http://jellyfin.test/"
    client._apikey = "test-key"
    client.user = "user"
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr(client, "_request", Mock(return_value=Mock(get_res=Mock(return_value=response))))
    return client


@pytest.mark.parametrize("path", [None, "", "/media/电影.mkv"])
def test_playback_path_preserves_nullable_text(monkeypatch: pytest.MonkeyPatch, path: object) -> None:
    """
    播放源路径的空值及空字符串原样返回，不产生字符串 None。
    """
    client = _client(monkeypatch, {"MediaSources": [{"Path": path}]})
    assert client.get_item_path_by_id("item") == path


@pytest.mark.parametrize("identifier", [None, "user-id", 123])
def test_named_user_preserves_identifier_type(monkeypatch: pytest.MonkeyPatch, identifier: object) -> None:
    """
    按名称命中的用户标识保持字符串、整数或空值原样。
    """
    client = _client(monkeypatch, [{"Name": "admin", "Id": identifier}])
    assert client.get_user("admin") == identifier


def test_invalid_path_uses_existing_request_failure_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    非文本路径在响应边界被拒绝，并由既有请求异常处理返回空值。
    """
    client = _client(monkeypatch, {"MediaSources": [{"Path": ["invalid"]}]})
    assert client.get_item_path_by_id("item") is None


def test_ancestor_field_preserves_arbitrary_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    通用祖先字段读取保留嵌套 JSON，而不误标成纯字符串。
    """
    nested = {"Keys": [1, None, "value"]}
    client = _client(monkeypatch, [{"Custom": nested}])
    assert client.get_itemId_ancestors("item", 0, "Custom") is nested


@pytest.mark.parametrize("method_name", ["get_data", "post_data"])
def test_custom_request_integer_user_preserves_type_error(
    monkeypatch: pytest.MonkeyPatch, method_name: str,
) -> None:
    """
    非零整数用户标识仍在 URL 替换阶段抛出 TypeError，不被请求异常处理吞掉。
    """
    client = _client(monkeypatch, {})
    client.user = 123
    with pytest.raises(TypeError):
        getattr(client, method_name)("[HOST]Users/[USER]?api_key=[APIKEY]")
    client._request.assert_not_called()


@pytest.mark.parametrize("method_name", ["get_data", "post_data"])
def test_custom_request_zero_user_preserves_empty_replacement(
    monkeypatch: pytest.MonkeyPatch, method_name: str,
) -> None:
    """
    值为零的用户标识沿用假值回退，替换为空字符串后正常发送请求。
    """
    client = _client(monkeypatch, {})
    client.user = 0
    result = getattr(client, method_name)("[HOST]Users/[USER]?api_key=[APIKEY]")
    request = client._request.return_value
    sender = request.get_res if method_name == "get_data" else request.post_res
    assert result is sender.return_value
    assert sender.call_args.kwargs["url"] == "http://jellyfin.test/Users/?api_key=test-key"


def test_library_folders_missing_paths_preserves_type_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    媒体库缺少路径列表时仍抛出 TypeError，不能误判为已成功读取空库。
    """
    client = _client(monkeypatch, {})
    monkeypatch.setattr(client, "get_jellyfin_virtual_folders", Mock(return_value=[{"Id": "library"}]))
    with pytest.raises(TypeError):
        client.get_user_library_folders()


def test_library_folders_empty_paths_preserves_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    合法空路径列表表示没有同步目录，应返回空列表而不抛出异常。
    """
    client = _client(monkeypatch, {})
    monkeypatch.setattr(
        client, "get_jellyfin_virtual_folders", Mock(return_value=[{"Id": "library", "Path": []}]),
    )
    assert client.get_user_library_folders() == []
