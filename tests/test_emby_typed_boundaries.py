"""
验证 Emby JSON 边界保留用户标识、路径和错误语义。
"""

from unittest.mock import Mock

import pytest

from app.modules.emby.emby import Emby


def _client(monkeypatch: pytest.MonkeyPatch, payload: object) -> Emby:
    """
    构造注入离线响应的 Emby 客户端。
    """
    client = Emby.__new__(Emby)
    client._host = "http://emby.test/"
    client._apikey = "test-key"
    client.user = "user"
    client._username = None
    client._sync_libraries = []
    response = Mock()
    response.json.return_value = payload
    request = Mock(get_res=Mock(return_value=response))
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils", Mock(return_value=request))
    return client


@pytest.mark.parametrize("identifier", [None, "user-id", 123])
def test_named_user_preserves_identifier_type(monkeypatch: pytest.MonkeyPatch, identifier: object) -> None:
    """
    按名称命中的用户标识保持字符串、整数或空值原样。
    """
    client = _client(monkeypatch, [{"Name": "admin", "Id": identifier}])
    assert client.get_user("admin") == identifier


@pytest.mark.parametrize("value", [None, "", "/media/movie.mkv"])
def test_server_id_preserves_nullable_text(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """
    服务器标识的空值及文本值不被强制转换。
    """
    client = _client(monkeypatch, {"Id": value})
    assert client.get_server_id() == (value or None)


def test_get_user_count_rejects_non_integer_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用户数量响应为非整数时进入现有异常处理并返回空值。
    """
    client = _client(monkeypatch, {"TotalRecordCount": "many"})
    assert client.get_user_count() == 0


def test_custom_request_integer_user_preserves_type_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    非零整数用户标识仍在 URL 替换阶段抛出 TypeError。
    """
    client = _client(monkeypatch, {})
    client.user = 123
    with pytest.raises(TypeError):
        client.get_data("[HOST]Users/[USER]?api_key=[APIKEY]")
    # URL 替换在构造请求前失败，因此不会建立请求对象。



def test_library_folders_missing_and_empty_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    缺少路径列表保留 TypeError，合法空路径列表返回空结果。
    """
    client = _client(monkeypatch, {})
    monkeypatch.setattr(client, "get_emby_virtual_folders", Mock(return_value=[{"Id": "library"}]))
    with pytest.raises(TypeError):
        client.get_user_library_folders()
    monkeypatch.setattr(client, "get_emby_virtual_folders", Mock(return_value=[{"Id": "library", "Path": []}]))
    assert client.get_user_library_folders() == []
