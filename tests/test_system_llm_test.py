import asyncio
import sys
import unittest
from types import ModuleType
from unittest.mock import AsyncMock, patch


def _stub_module(name: str, **attrs):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _DummyError(Exception):
    def __init__(self, message="", duration_ms=None):
        super().__init__(message)
        self.duration_ms = duration_ms


for _module_name in ("pillow_avif", "aiofiles", "psutil"):
    _stub_module(_module_name)

_stub_module("app.helper.sites", SitesHelper=_Dummy)
_stub_module("app.chain.mediaserver", MediaServerChain=_Dummy)
_stub_module("app.chain.search", SearchChain=_Dummy)
_stub_module("app.chain.system", SystemChain=_Dummy)
_stub_module("app.core.event", eventmanager=_Dummy())
_stub_module("app.core.metainfo", MetaInfo=_Dummy)
_stub_module("app.core.module", ModuleManager=_Dummy)
_stub_module(
    "app.core.security",
    verify_apitoken=_Dummy,
    verify_resource_token=_Dummy,
    verify_token=_Dummy,
)
_stub_module("app.db.models", User=_Dummy)
_stub_module("app.db.systemconfig_oper", SystemConfigOper=_Dummy)
_stub_module(
    "app.db.user_oper",
    get_current_active_superuser=_Dummy,
    get_current_active_superuser_async=_Dummy,
    get_current_active_user_async=_Dummy,
)
_stub_module(
    "app.helper.llm",
    LLMHelper=_Dummy,
    LLMTestError=_DummyError,
    LLMTestTimeout=_DummyError,
)
_stub_module("app.helper.mediaserver", MediaServerHelper=_Dummy)
_stub_module("app.helper.message", MessageHelper=_Dummy)
_stub_module("app.helper.progress", ProgressHelper=_Dummy)
_stub_module("app.helper.rule", RuleHelper=_Dummy)
_stub_module("app.helper.subscribe", SubscribeHelper=_Dummy)
_stub_module("app.helper.system", SystemHelper=_Dummy)
_stub_module("app.helper.image", ImageHelper=_Dummy)
_stub_module("app.scheduler", Scheduler=_Dummy)
_stub_module(
    "app.log",
    logger=_Dummy(),
    log_settings=_Dummy(),
    LogConfigModel=type("LogConfigModel", (), {}),
)
_stub_module("app.utils.crypto", HashUtils=_Dummy)
_stub_module("app.utils.http", RequestUtils=_Dummy, AsyncRequestUtils=_Dummy)
_stub_module("version", APP_VERSION="test")

from app.api.endpoints import system as system_endpoint


class LlmTestEndpointTest(unittest.TestCase):
    def test_llm_test_requires_ai_agent_enabled(self):
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", False):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertEqual(resp.message, "请先启用智能助手")

    def test_llm_test_requires_api_key(self):
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_API_KEY", None
        ), patch.object(system_endpoint.settings, "LLM_MODEL", "deepseek-chat"):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertEqual(resp.message, "请先配置 LLM API Key")
        self.assertEqual(resp.data["model"], "deepseek-chat")

    def test_llm_test_requires_model(self):
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_API_KEY", "sk-test"
        ), patch.object(system_endpoint.settings, "LLM_MODEL", ""):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertEqual(resp.message, "请先配置 LLM 模型")

    def test_llm_test_returns_successful_reply_preview(self):
        llm_test_mock = AsyncMock(
            return_value={
                "provider": "deepseek",
                "model": "deepseek-chat",
                "duration_ms": 321,
                "reply_preview": "OK",
            }
        )
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_PROVIDER", "deepseek"
        ), patch.object(system_endpoint.settings, "LLM_MODEL", "deepseek-chat"), patch.object(
            system_endpoint.settings, "LLM_API_KEY", "sk-test"
        ), patch.object(
            system_endpoint.settings, "LLM_BASE_URL", "https://api.deepseek.com"
        ), patch.object(
            system_endpoint.LLMHelper,
            "test_current_settings",
            llm_test_mock,
            create=True,
        ):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        llm_test_mock.assert_awaited_once_with(
            provider="deepseek",
            model="deepseek-chat",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
        )
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["provider"], "deepseek")
        self.assertEqual(resp.data["model"], "deepseek-chat")
        self.assertEqual(resp.data["duration_ms"], 321)
        self.assertEqual(resp.data["reply_preview"], "OK")

    def test_llm_test_rejects_empty_reply(self):
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_PROVIDER", "deepseek"
        ), patch.object(system_endpoint.settings, "LLM_MODEL", "deepseek-chat"), patch.object(
            system_endpoint.settings, "LLM_API_KEY", "sk-test"
        ), patch.object(
            system_endpoint.LLMHelper,
            "test_current_settings",
            AsyncMock(return_value={"provider": "deepseek", "model": "deepseek-chat", "duration_ms": 12}),
            create=True,
        ):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertEqual(resp.message, "模型响应为空")
        self.assertEqual(resp.data["duration_ms"], 12)

    def test_llm_test_maps_timeout_error(self):
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_PROVIDER", "deepseek"
        ), patch.object(system_endpoint.settings, "LLM_MODEL", "deepseek-chat"), patch.object(
            system_endpoint.settings, "LLM_API_KEY", "sk-test"
        ), patch.object(
            system_endpoint.LLMHelper,
            "test_current_settings",
            AsyncMock(side_effect=TimeoutError("request timed out")),
            create=True,
        ):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertEqual(resp.message, "LLM 调用超时")

    def test_llm_test_sanitizes_error_message(self):
        raw_error = (
            "request failed api_key=sk-secret "
            "Authorization: Bearer sk-secret "
            "base error sk-secret"
        )
        with patch.object(system_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
            system_endpoint.settings, "LLM_API_KEY", "sk-secret"
        ), patch.object(system_endpoint.settings, "LLM_PROVIDER", "deepseek"), patch.object(
            system_endpoint.settings, "LLM_MODEL", "deepseek-chat"
        ), patch.object(
            system_endpoint.LLMHelper,
            "test_current_settings",
            AsyncMock(side_effect=RuntimeError(raw_error)),
            create=True,
        ):
            resp = asyncio.run(system_endpoint.llm_test(_="token"))

        self.assertFalse(resp.success)
        self.assertNotIn("sk-secret", resp.message)
        self.assertNotIn("Authorization: Bearer", resp.message)
        self.assertIn("***", resp.message)


if __name__ == "__main__":
    unittest.main()
