"""
验证 Jellyfin 用户选择、媒体库查询及测试模块隔离。
"""

import sys
from unittest.mock import patch

from app.modules.jellyfin import jellyfin as jellyfin_module
from app.modules.jellyfin.jellyfin import Jellyfin


class _FakeResponse:
    """
    保存预设接口响应并提供 JSON 读取能力。
    """

    def __init__(self, payload: dict):
        """
        保存模拟接口的响应内容与状态，避免实际网络请求。
        """
        self._payload = payload

    def json(self):
        """
        返回预设响应正文，不发起实际请求。
        """
        return self._payload


def test_loader_does_not_leave_stub_modules_in_sys_modules() -> None:
    """
    加载 Jellyfin 测试模块后不残留替换依赖模块。
    """
    assert "_test_jellyfin_module" not in sys.modules
    assert not getattr(sys.modules.get("app.runtime.log"), "_jellyfin_test_stub", False)
    assert not getattr(sys.modules.get("app.runtime.config"), "_jellyfin_test_stub", False)
    assert not getattr(sys.modules.get("app.adapters.network.http"), "_jellyfin_test_stub", False)


def _build_client() -> Jellyfin:
    """
    构造绕过网络初始化的 Jellyfin 客户端，允许直接注入接口响应。
    """
    client = Jellyfin.__new__(Jellyfin)
    client._host = "http://jellyfin.local:8096"
    client._apikey = "api-key"
    client._playhost = None
    client._sync_libraries = []
    client.user = "fallback-user"
    return client


def test_get_user_prefers_exact_username_without_warning() -> None:
    """
    用户名精确匹配时直接选择该用户且不发出警告。
    """
    client = _build_client()
    payload = [
        {"Id": "admin-id", "Name": "admin", "Policy": {"IsAdministrator": True}},
        {"Id": "alice-id", "Name": "alice", "Policy": {"IsAdministrator": False}},
    ]

    with (
        patch.object(jellyfin_module, "RequestUtils") as request_utils_cls,
        patch.object(jellyfin_module.logger, "warning") as warning_mock,
    ):
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

        user_id = client.get_user("alice")

    assert user_id == "alice-id"
    warning_mock.assert_not_called()


def test_get_user_prefers_enable_all_folders_admin() -> None:
    """
    没有精确匹配时优先选择可访问全部媒体库的管理员。
    """
    client = _build_client()
    payload = [
        {
            "Id": "visible-admin-id",
            "Name": "visible",
            "Policy": {
                "IsAdministrator": True,
                "EnabledFolders": ["lib-1", "lib-2", "lib-3"],
            },
        },
        {
            "Id": "full-admin-id",
            "Name": "full",
            "Policy": {"IsAdministrator": True, "EnableAllFolders": True},
        },
    ]

    with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

        user_id = client.get_user()

    assert user_id == "full-admin-id"


def test_get_user_warns_and_prefers_larger_visible_scope_admin() -> None:
    """
    多个管理员候选时优先可见范围较大者并提示歧义。
    """
    client = _build_client()
    payload = [
        {
            "Id": "small-admin-id",
            "Name": "small",
            "Policy": {"IsAdministrator": True, "EnabledFolders": ["lib-1"]},
        },
        {
            "Id": "large-admin-id",
            "Name": "large",
            "Policy": {
                "IsAdministrator": True,
                "EnabledFolders": ["lib-1", "lib-2", "lib-3"],
            },
        },
        {"Id": "user-id", "Name": "normal", "Policy": {"IsAdministrator": False}},
    ]

    with (
        patch.object(jellyfin_module, "RequestUtils") as request_utils_cls,
        patch.object(jellyfin_module.logger, "warning") as warning_mock,
    ):
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

        user_id = client.get_user("admin")

    assert user_id == "large-admin-id"
    assert warning_mock.call_count >= 2

    warning_messages = [
        call.args[0] for call in warning_mock.call_args_list if call.args and isinstance(call.args[0], str)
    ]
    assert any(("超级管理员" in message for message in warning_messages))
    assert any(
        (
            "部分" in message or "可见" in message or "访问范围" in message or ("EnabledFolders" in message)
            for message in warning_messages
        )
    )
    assert any(("回退" in message or "fallback" in message.lower() for message in warning_messages))


def test_get_jellyfin_librarys_reports_failure_when_user_missing() -> None:
    """
    缺少有效用户时媒体库查询返回失败。
    """
    client = _build_client()
    client.user = None

    with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
        libraries = client._Jellyfin__get_jellyfin_librarys()

    assert libraries is None
    request_utils_cls.assert_not_called()


def test_get_jellyfin_librarys_uses_normalized_views_url() -> None:
    """
    媒体库查询使用规范化的用户视图 URL。
    """
    client = _build_client()
    client._host = "http://jellyfin.local:8096"
    client.user = "user-id"

    with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse({"Items": []})

        libraries = client._Jellyfin__get_jellyfin_librarys()

    assert libraries == []
    request_utils_cls.return_value.get_res.assert_called_once_with(
        "http://jellyfin.local:8096/Users/user-id/Views",
        {"api_key": "api-key"},
    )
