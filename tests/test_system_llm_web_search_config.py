"""系统 LLM 服务端联网搜索配置测试。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from app.api.endpoints import system as system_endpoint
from app.application.configuration import (
    get_configured_system_config,
    get_runtime_settings,
)
from app.runtime.config import settings
from app.startup.composition.system import compose_system_service


def _runtime():
    """构造使用真实 LLM 能力适配器的最小系统运行时。"""

    @asynccontextmanager
    async def rule_group_mutation():
        """提供本组测试不会进入的规则组事务替身。"""
        yield SimpleNamespace()

    return SimpleNamespace(
        system=compose_system_service(
            settings=get_runtime_settings(),
            system_config=get_configured_system_config(),
            rule_group_mutation=rule_group_mutation,
        )
    )


def test_set_env_rejects_unsupported_builtin_web_search() -> None:
    """强制不可用的服务端搜索时应拒绝保存且不部分写入配置。"""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "deepseek-chat",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_WEB_SEARCH_MODE": "builtin",
    }

    with patch.object(type(settings), "update_settings") as update_settings:
        response = asyncio.run(
            system_endpoint.set_env_setting(env=env, _=object(), runtime=_runtime())
        )

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
        type(settings),
        "update_settings",
        return_value={key: (True, None) for key in env},
    ) as update_settings, patch(
        "app.startup.composition.system.eventmanager.async_send_event"
    ):
        response = asyncio.run(
            system_endpoint.set_env_setting(env=env, _=object(), runtime=_runtime())
        )

    assert response.success is True
    update_settings.assert_called_once_with(env=env)
