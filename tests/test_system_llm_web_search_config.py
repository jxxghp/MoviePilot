"""系统 LLM 服务端联网搜索配置测试。"""

import asyncio
from unittest.mock import patch

from app.api.endpoints import system as system_endpoint


def test_set_env_rejects_unsupported_builtin_web_search() -> None:
    """强制不可用的服务端搜索时应拒绝保存且不部分写入配置。"""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "deepseek-chat",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_WEB_SEARCH_MODE": "builtin",
    }

    with patch.object(type(system_endpoint.settings), "update_settings") as update_settings:
        response = asyncio.run(system_endpoint.set_env_setting(env=env, _=object()))

    assert response.success is False
    assert "不支持服务端联网搜索" in response.message
    update_settings.assert_not_called()


def test_set_env_accepts_supported_deepseek_builtin_web_search() -> None:
    """DeepSeek V4 Flash 官方端点应允许保存强制服务端搜索。"""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "deepseek-v4-flash",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_WEB_SEARCH_MODE": "builtin",
    }

    with patch.object(
        type(system_endpoint.settings),
        "update_settings",
        return_value={key: (True, None) for key in env},
    ) as update_settings, patch.object(
        system_endpoint.eventmanager,
        "async_send_event",
    ):
        response = asyncio.run(system_endpoint.set_env_setting(env=env, _=object()))

    assert response.success is True
    update_settings.assert_called_once_with(env=env)
