"""
极影视外部响应边界及自定义请求失败语义回归。
"""

from unittest.mock import Mock, patch

import pytest

from app.modules.zspace.zspace import ZSpace


def _client() -> ZSpace:
    """
    构造不访问网络的已配置客户端。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "token"
    client._sync_libraries = []
    client.user = "user"
    return client


@pytest.mark.parametrize("payload", [{"bad": True}, ["bad"]])
def test_invalid_selectable_folders_uses_existing_views_fallback(payload):
    """
    目录响应形状错误时继续使用现有视图回退，保留视图的扩展字段。
    """
    client = _client()
    views = [{"Id": "library", "Extra": {"enabled": True}}]
    response = Mock()
    response.json.return_value = payload
    with patch("app.modules.zspace.zspace.RequestUtils") as transport, patch.object(
        client, "_ZSpace__get_library_views", return_value=views
    ) as get_views:
        transport.return_value.get_res.return_value = response
        assert client.get_library_folders() == views
    get_views.assert_called_once_with()


@pytest.mark.parametrize("method", ["get_data", "post_data"])
@pytest.mark.parametrize("user", [5, 0, None, "user"])
def test_custom_requests_preserve_user_placeholder_type_error(method, user):
    """
    非零整数用户仍在请求前抛出 TypeError，空用户仍替换为空文本。
    """
    client = _client()
    client.user = user
    with patch("app.modules.zspace.zspace.RequestUtils") as transport:
        if user == 5:
            with pytest.raises(TypeError, match="replace"):
                getattr(client, method)("[HOST][USER]")
            transport.assert_not_called()
        else:
            getattr(client, method)("[HOST][USER]")
            request = transport.return_value.get_res if method == "get_data" else transport.return_value.post_res
            assert request.call_args.kwargs["url"] == f"http://zspace.local/{user or ''}"


def test_missing_library_path_still_raises_outside_request_handling():
    """
    缺失路径仍抛出原有 TypeError，不静默跳过错误的库信息。
    """
    client = _client()
    with patch.object(client, "get_virtual_folders", return_value=[{"Id": "library"}]):
        with pytest.raises(TypeError, match="NoneType"):
            client.get_user_library_folders()


@pytest.mark.parametrize("identifier", ["user", 42, None])
def test_current_user_identity_preserves_integer_and_null(identifier):
    """
    当前用户查询保留整数、文本和空标识，不转换为字符串。
    """
    client = _client()
    with patch.object(client, "_ZSpace__get_current_user", return_value={"Id": identifier}):
        assert client._ZSpace__get_current_user_id() == identifier


def test_invalid_server_identifier_uses_existing_error_result():
    """
    非文本服务器标识走现有异常处理结果，不产生虚假的文本标识。
    """
    client = _client()
    response = Mock()
    response.json.return_value = {"Id": ["invalid"]}
    with patch("app.modules.zspace.zspace.RequestUtils") as transport:
        transport.return_value.get_res.return_value = response
        assert client.get_server_id() is None
