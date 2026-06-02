from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.helper.server import MoviePilotServerHelper


class MoviePilotServerHelperTests(unittest.TestCase):
    """
    MoviePilot 服务端请求辅助工具测试。
    """

    def setUp(self) -> None:
        """
        清理安装用户 ID 缓存，避免不同用例之间互相影响。
        """
        MoviePilotServerHelper._user_uid = None

    def test_server_request_adds_user_uid_header(self):
        """
        发往 MoviePilot 服务端的请求会自动携带安装用户 ID。
        """
        with patch.object(MoviePilotServerHelper, "get_user_uid", return_value="uid-1"), \
                patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"):
            headers = MoviePilotServerHelper.build_headers(
                "https://movie-pilot.org/plugin/install",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(headers["X-MoviePilot-User-Uid"], "uid-1")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_non_server_request_does_not_add_user_uid_header(self):
        """
        发往其他域名的请求不会携带安装用户 ID。
        """
        with patch.object(MoviePilotServerHelper, "get_user_uid", return_value="uid-1"), \
                patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"):
            headers = MoviePilotServerHelper.build_headers(
                "https://example.com/plugin/install",
                headers={"Content-Type": "application/json"},
            )

        self.assertNotIn("X-MoviePilot-User-Uid", headers)

    def test_existing_user_uid_header_is_preserved(self):
        """
        调用方显式传入的安装用户 ID 请求头不被覆盖。
        """
        with patch.object(MoviePilotServerHelper, "get_user_uid", return_value="uid-1"), \
                patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"):
            headers = MoviePilotServerHelper.build_headers(
                "https://movie-pilot.org/plugin/install",
                headers={
                    "Content-Type": "application/json",
                    "X-MoviePilot-User-Uid": "custom-uid",
                },
            )

        self.assertEqual(headers["X-MoviePilot-User-Uid"], "custom-uid")

    def test_existing_user_uid_header_is_detected_case_insensitively(self):
        """
        调用方使用不同大小写的安装用户 ID 请求头时不会重复注入。
        """
        with patch.object(MoviePilotServerHelper, "get_user_uid", return_value="uid-1"), \
                patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"):
            headers = MoviePilotServerHelper.build_headers(
                "https://movie-pilot.org/plugin/install",
                headers={
                    "Content-Type": "application/json",
                    "x-moviepilot-user-uid": "custom-uid",
                },
            )

        self.assertNotIn("X-MoviePilot-User-Uid", headers)
        self.assertEqual(headers["x-moviepilot-user-uid"], "custom-uid")

    def test_content_type_can_be_added(self):
        """
        构建 JSON 请求头时会补充 Content-Type。
        """
        with patch.object(MoviePilotServerHelper, "get_user_uid", return_value="uid-1"), \
                patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"):
            headers = MoviePilotServerHelper.build_headers(
                "https://movie-pilot.org/plugin/install",
                content_type="application/json",
            )

        self.assertEqual(headers["Content-Type"], "application/json")

    def test_subscribe_fork_uses_fork_endpoint(self):
        """
        订阅复用请求使用服务端 fork 接口。
        """
        with patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"), \
                patch.object(MoviePilotServerHelper, "_get", return_value=None) as request:
            MoviePilotServerHelper.subscribe_fork(9)

        request.assert_called_once_with(
            "https://movie-pilot.org/subscribe/fork/9",
            timeout=5,
        )

    def test_workflow_fork_uses_fork_endpoint(self):
        """
        工作流复用请求使用服务端 fork 接口。
        """
        with patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"), \
                patch.object(MoviePilotServerHelper, "_get", return_value=None) as request:
            MoviePilotServerHelper.workflow_fork(9)

        request.assert_called_once_with(
            "https://movie-pilot.org/workflow/fork/9",
            timeout=5,
        )

    def test_user_permissions_uses_server_endpoint(self):
        """
        用户权限请求使用服务端权限接口。
        """
        with patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"), \
                patch.object(MoviePilotServerHelper, "_get", return_value=None) as request:
            MoviePilotServerHelper.user_permissions("jxxghp")

        request.assert_called_once_with(
            "https://movie-pilot.org/user/permissions",
            params={"github_user": "jxxghp"},
            include_user_uid=False,
            timeout=5,
        )

    def test_is_admin_user_uses_server_permissions(self):
        """
        共享管理权限由服务端权限结果决定。
        """
        response = Mock(status_code=200)
        response.json.return_value = {"is_admin": True}
        with patch.object(MoviePilotServerHelper, "get_github_user", return_value="jxxghp"), \
                patch.object(MoviePilotServerHelper, "user_permissions", return_value=response):
            self.assertTrue(MoviePilotServerHelper.is_admin_user())

    def test_is_admin_user_returns_false_without_server_permission(self):
        """
        服务端未返回管理权限时不授予共享管理权限。
        """
        response = Mock(status_code=200)
        response.json.return_value = {"is_admin": False}
        with patch.object(MoviePilotServerHelper, "get_github_user", return_value="user"), \
                patch.object(MoviePilotServerHelper, "user_permissions", return_value=response):
            self.assertFalse(MoviePilotServerHelper.is_admin_user())
