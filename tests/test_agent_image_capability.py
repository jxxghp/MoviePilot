from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import MoviePilotAgent
from app.agent.llm import AgentCapabilityManager, LLMHelper, LLMProviderManager
from app.application.orchestration.message import MessageChain
from app.runtime.config import settings
from app.schemas.types import NotificationChannel


@pytest.fixture
def stub_llm_model_catalog(monkeypatch):
    """打桩 models.dev 目录查询边界，离线文件随仓库保持为空，不依赖真实目录数据。"""
    catalog = {
        ("minimax", "MiniMax-M2.7"): {
            "modalities": {"input": ["text"], "output": ["text"]},
        },
        ("zhipuai", "glm-5v-turbo"): {
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        },
    }

    def fake_resolve(self, provider_id, model_id, base_url=None, base_url_preset_id=None):
        """按 provider 与模型标识返回目录元数据，未知模型返回 None。"""
        return catalog.get((provider_id, model_id))

    monkeypatch.setattr(
        LLMProviderManager, "resolve_cached_model_metadata", fake_resolve
    )


def test_llm_supports_image_input_uses_model_catalog_text_only(
        monkeypatch, stub_llm_model_catalog
):
    """内置目录明确为纯文本模型时，应自动关闭图片输入。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)

    assert not LLMHelper.supports_image_input(
        provider="minimax",
        model="MiniMax-M2.7",
    )


def test_llm_supports_image_input_keeps_known_vision_model(
        monkeypatch, stub_llm_model_catalog
):
    """内置目录明确为视觉模型时，应允许图片输入。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)

    assert LLMHelper.supports_image_input(
        provider="zhipuai",
        model="glm-5v-turbo",
    )


def test_llm_supports_image_input_keeps_unknown_model_override(monkeypatch):
    """未知自定义模型保持用户开关语义，避免误伤私有视觉模型。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)

    assert LLMHelper.supports_image_input(
        provider="custom-provider",
        model="custom-vlm-model",
    )


def test_agent_capability_manager_delegates_image_support():
    """Agent 能力管理器应复用统一的模型图片能力判断。"""
    with patch.object(LLMHelper, "supports_image_input", return_value=False) as supports:
        assert not AgentCapabilityManager.supports_image_input()

    supports.assert_called_once_with()


def test_handle_ai_message_routes_text_only_model_images_to_files(
        monkeypatch, stub_llm_model_catalog
):
    """纯文本模型收到图片消息时，应降级为文件附件而非 image_url 内容块。"""
    chain = MessageChain()
    chain.runtime_config = replace(
        chain.runtime_config,
        ai_agent_enable=True,
        llm_provider="minimax",
        llm_model="MiniMax-M2.7",
    )
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "minimax")
    monkeypatch.setattr(settings, "LLM_MODEL", "MiniMax-M2.7")
    # 测试绕过完整启动组合根，按需装配 llm_helper provider 以走真实能力判断
    import app.application.agent as agent_facade

    monkeypatch.setattr(
        agent_facade, "_llm_helper_provider", lambda: LLMHelper
    )

    with patch.object(
        chain, "_get_or_create_session_id", return_value="session-1"
    ), patch.object(
        chain, "_download_attachments_to_data_urls"
    ) as download_images, patch.object(
        chain,
        "_prepare_agent_files",
        return_value=[
            {
                "name": "image_1.jpg",
                "mime_type": "image/jpeg",
                "local_path": "/tmp/image_1.jpg",
                "status": "ready",
            }
        ],
    ) as prepare_files, patch(
        "app.application.orchestration.message.get_running_agent_manager"
    ) as get_running_manager, patch(
        "app.application.orchestration.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: coro.close(),
    ):
        process_message = AsyncMock()
        get_running_manager.return_value.process_message = process_message
        chain._handle_ai_message(
            text="/ai 帮我看看这张图",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            images=["tg://file_id/image-1"],
        )

    download_images.assert_not_called()
    prepare_files.assert_called_once()
    assert prepare_files.call_args.kwargs["files"][0].ref == "tg://file_id/image-1"
    assert process_message.call_args.kwargs["images"] is None
    assert process_message.call_args.kwargs["files"][0]["local_path"] == "/tmp/image_1.jpg"


def test_unsupported_image_error_recognizes_vlm_text_only_message():
    """兼容端点返回 not a VLM 时，应识别为图片输入能力错误。"""
    error = Exception(
        "Error code: 400 - {'code': 20041, 'message': "
        "'The model is not a VLM (Vision Language Model). "
        "Please use text-only prompts.'}"
    )

    assert MoviePilotAgent._is_unsupported_image_input_error(error)
