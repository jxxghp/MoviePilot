"""Agent 资源流程工具权限测试。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.edit_file import EditFileTool
from app.agent.tools.impl.list_directory import ListDirectoryTool
from app.agent.tools.impl.query_downloaders import QueryDownloadersTool
from app.agent.tools.impl.query_sites import QuerySitesTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.write_file import WriteFileTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent import MoviePilotAgent
from app.core.config import settings
from app.schemas.types import MessageChannel


def test_non_admin_manager_exposes_resource_flow_helper_tools():
    """普通用户应能看到搜索、订阅、下载流程所需的辅助工具。"""
    site_tool = QuerySitesTool(session_id="session-1", user_id="10001")
    downloader_tool = QueryDownloadersTool(session_id="session-1", user_id="10001")

    with patch(
        "app.agent.tools.manager.MoviePilotToolFactory.create_tools",
        return_value=[site_tool, downloader_tool],
    ):
        manager = MoviePilotToolsManager(is_admin=False)

    tool_names = {tool.name for tool in manager.list_tools()}
    assert "query_sites" in tool_names
    assert "query_downloaders" in tool_names


def test_non_admin_manager_exposes_restricted_file_tools():
    """普通用户应能看到受目录边界限制的文件读写工具。"""
    tools = [
        ReadFileTool(session_id="session-1", user_id="10001"),
        WriteFileTool(session_id="session-1", user_id="10001"),
        EditFileTool(session_id="session-1", user_id="10001"),
        ListDirectoryTool(session_id="session-1", user_id="10001"),
    ]

    with patch(
        "app.agent.tools.manager.MoviePilotToolFactory.create_tools",
        return_value=tools,
    ):
        manager = MoviePilotToolsManager(is_admin=False)

    tool_names = {tool.name for tool in manager.list_tools()}
    assert {"read_file", "write_file", "edit_file", "list_directory"} <= tool_names


def test_non_admin_manager_hides_admin_only_send_local_file_tool():
    """普通用户不能看到仅管理员可用的本地附件发送工具。"""
    manager = MoviePilotToolsManager(is_admin=False)

    tool_names = {tool.name for tool in manager.list_tools()}

    assert "send_local_file" not in tool_names


def test_query_sites_hides_only_sensitive_fields_for_non_admin_user():
    """普通用户查询站点时只隐藏 Cookie、API Key、Token 和 RSS。"""
    tool = QuerySitesTool(session_id="session-1", user_id="10001")
    site = SimpleNamespace(
        id=1,
        name="TestSite",
        domain="secret.example",
        url="https://secret.example/",
        pri=1,
        rss="https://secret.example/rss",
        cookie="uid=1; passkey=secret",
        ua="SecretUA",
        apikey="site-api-key",
        token="site-token",
        proxy=1,
        filter="",
        render=0,
        public=0,
        note={"secret": True},
        limit_interval=0,
        limit_count=0,
        limit_seconds=0,
        timeout=15,
        is_active=True,
        downloader="qb",
    )

    with patch(
        "app.agent.tools.impl.query_sites.SiteOper"
    ) as site_oper:
        site_oper.return_value.async_list = AsyncMock(return_value=[site])
        result = asyncio.run(tool.run())

    payload = json.loads(result)
    assert payload == [
        {
            "id": 1,
            "name": "TestSite",
            "domain": "secret.example",
            "url": "https://secret.example/",
            "pri": 1,
            "is_active": True,
            "downloader": "qb",
            "ua": "SecretUA",
            "proxy": 1,
            "filter": "",
            "render": 0,
            "public": 0,
            "note": {"secret": True},
            "limit_interval": 0,
            "limit_count": 0,
            "limit_seconds": 0,
            "timeout": 15,
        }
    ]
    assert "cookie" not in payload[0]
    assert "rss" not in payload[0]
    assert "token" not in payload[0]
    assert "apikey" not in payload[0]


def test_query_sites_keeps_full_fields_for_admin_context():
    """管理员查询站点时保留完整配置视图。"""
    tool = QuerySitesTool(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    site = SimpleNamespace(
        id=1,
        name="TestSite",
        domain="secret.example",
        url="https://secret.example/",
        pri=1,
        rss="https://secret.example/rss",
        cookie="uid=1; passkey=secret",
        ua="SecretUA",
        apikey="site-api-key",
        token="site-token",
        proxy=1,
        filter="",
        render=0,
        public=0,
        note={"secret": True},
        limit_interval=0,
        limit_count=0,
        limit_seconds=0,
        timeout=15,
        is_active=True,
        downloader="qb",
    )

    with patch(
        "app.agent.tools.impl.query_sites.SiteOper"
    ) as site_oper:
        site_oper.return_value.async_list = AsyncMock(return_value=[site])
        result = asyncio.run(tool.run())

    payload = json.loads(result)
    assert payload[0]["cookie"] == "uid=1; passkey=secret"
    assert payload[0]["token"] == "site-token"
    assert payload[0]["apikey"] == "site-api-key"
    assert payload[0]["url"] == "https://secret.example/"


def test_non_admin_file_tools_can_access_config_directory(tmp_path, monkeypatch):
    """普通用户可在配置目录内读写和编辑文件。"""
    config_path = tmp_path / "config"
    monkeypatch.setattr(settings, "CONFIG_DIR", str(config_path))
    memory_path = settings.CONFIG_PATH / "agent" / "memory" / "MEMORY.md"

    write_tool = WriteFileTool(session_id="session-1", user_id="10001")
    read_tool = ReadFileTool(session_id="session-1", user_id="10001")
    edit_tool = EditFileTool(session_id="session-1", user_id="10001")

    write_result = asyncio.run(write_tool.run(str(memory_path), "hello"))
    read_result = asyncio.run(read_tool.run(str(memory_path)))
    edit_result = asyncio.run(edit_tool.run(str(memory_path), "hello", "hello mp"))
    edited_content = memory_path.read_text(encoding="utf-8")

    assert "成功写入文件" in write_result
    assert read_result == "hello"
    assert "成功编辑文件" in edit_result
    assert edited_content == "hello mp"


def test_non_admin_file_tools_block_paths_outside_allowed_roots(
    tmp_path, monkeypatch
):
    """普通用户不能通过文件工具访问 Agent 配置目录外的路径。"""
    config_path = tmp_path / "config"
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_DIR", str(config_path))

    read_tool = ReadFileTool(session_id="session-1", user_id="10001")
    write_tool = WriteFileTool(session_id="session-1", user_id="10001")
    edit_tool = EditFileTool(session_id="session-1", user_id="10001")
    list_tool = ListDirectoryTool(session_id="session-1", user_id="10001")

    read_result = asyncio.run(read_tool.run(str(outside_path)))
    write_result = asyncio.run(write_tool.run(str(outside_path), "changed"))
    edit_result = asyncio.run(edit_tool.run(str(outside_path), "secret", "changed"))
    with patch.object(ListDirectoryTool, "_list_directory_sync") as list_directory:
        list_result = asyncio.run(list_tool.run(str(tmp_path)))

    assert "普通用户只能读取" in read_result
    assert "普通用户只能写入" in write_result
    assert "普通用户只能编辑" in edit_result
    assert "普通用户只能列出" in list_result
    assert "日志目录" not in read_result
    assert "日志目录" not in write_result
    assert "日志目录" not in edit_result
    assert "日志目录" not in list_result
    assert outside_path.read_text(encoding="utf-8") == "secret"
    list_directory.assert_not_called()


def test_non_admin_file_tools_block_log_directory(tmp_path, monkeypatch):
    """普通用户不能通过文件工具读写运行日志目录。"""
    config_path = tmp_path / "config"
    monkeypatch.setattr(settings, "CONFIG_DIR", str(config_path))
    log_path = settings.LOG_PATH / "moviepilot.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("secret log", encoding="utf-8")

    read_tool = ReadFileTool(session_id="session-1", user_id="10001")
    write_tool = WriteFileTool(session_id="session-1", user_id="10001")
    edit_tool = EditFileTool(session_id="session-1", user_id="10001")
    list_tool = ListDirectoryTool(session_id="session-1", user_id="10001")

    read_result = asyncio.run(read_tool.run(str(log_path)))
    write_result = asyncio.run(write_tool.run(str(log_path), "changed"))
    edit_result = asyncio.run(edit_tool.run(str(log_path), "secret log", "changed"))
    with patch.object(
        ListDirectoryTool, "_list_directory_sync", return_value="listed"
    ) as list_directory:
        list_result = asyncio.run(list_tool.run(str(settings.LOG_PATH)))

    assert "普通用户只能读取" in read_result
    assert "普通用户只能写入" in write_result
    assert "普通用户只能编辑" in edit_result
    assert "普通用户只能列出" in list_result
    assert "日志目录" not in read_result
    assert "日志目录" not in write_result
    assert "日志目录" not in edit_result
    assert "日志目录" not in list_result
    assert log_path.read_text(encoding="utf-8") == "secret log"
    list_directory.assert_not_called()


def test_admin_file_tool_can_access_paths_outside_allowed_roots(
    tmp_path, monkeypatch
):
    """管理员上下文不受普通用户文件访问边界限制。"""
    config_path = tmp_path / "config"
    outside_path = tmp_path / "outside.txt"
    monkeypatch.setattr(settings, "CONFIG_DIR", str(config_path))

    tool = WriteFileTool(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})

    result = asyncio.run(tool.run(str(outside_path), "admin write"))

    assert "成功写入文件" in result
    assert outside_path.read_text(encoding="utf-8") == "admin write"


def test_query_downloaders_hides_sensitive_fields_for_non_admin_user():
    """普通用户查询下载器时只返回选择下载器所需的安全字段。"""
    tool = QueryDownloadersTool(session_id="session-1", user_id="10001")
    downloaders = [
        {
            "name": "qb",
            "type": "qbittorrent",
            "enabled": True,
            "host": "http://127.0.0.1",
            "port": 8080,
            "username": "admin",
            "password": "secret",
            "apikey": "downloader-api-key",
            "token": "downloader-token",
        }
    ]

    with patch(
        "app.agent.tools.impl.query_downloaders.SystemConfigOper"
    ) as system_config_oper:
        system_config_oper.return_value.get.return_value = downloaders
        result = asyncio.run(tool.run())

    payload = json.loads(result)
    assert payload == [
        {
            "name": "qb",
            "type": "qbittorrent",
            "enabled": True,
        }
    ]
    assert "host" not in payload[0]
    assert "username" not in payload[0]
    assert "password" not in payload[0]
    assert "apikey" not in payload[0]
    assert "token" not in payload[0]


def test_query_downloaders_keeps_full_fields_for_admin_context():
    """管理员查询下载器时保留完整配置视图。"""
    tool = QueryDownloadersTool(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    downloaders = [
        {
            "name": "qb",
            "type": "qbittorrent",
            "enabled": True,
            "host": "http://127.0.0.1",
            "username": "admin",
            "password": "secret",
            "apikey": "downloader-api-key",
        }
    ]

    with patch(
        "app.agent.tools.impl.query_downloaders.SystemConfigOper"
    ) as system_config_oper:
        system_config_oper.return_value.get.return_value = downloaders
        result = asyncio.run(tool.run())

    payload = json.loads(result)
    assert payload[0]["host"] == "http://127.0.0.1"
    assert payload[0]["username"] == "admin"
    assert payload[0]["password"] == "secret"
    assert payload[0]["apikey"] == "downloader-api-key"


def test_channel_agent_admin_user_id_does_not_bypass_user_lookup():
    """渠道用户 ID 恰好为 admin 时，不应绕过真实系统用户权限判断。"""
    agent = MoviePilotAgent(
        session_id="session-1",
        user_id="admin",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="normal-user",
    )

    with patch("app.agent.UserOper") as user_oper:
        user_oper.return_value.async_get_by_name.return_value = SimpleNamespace(
            is_superuser=False
        )
        context = asyncio.run(
            agent._build_tool_context(should_dispatch_reply=True)
        )

    assert context["is_admin"] is False
