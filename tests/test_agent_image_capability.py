from unittest.mock import AsyncMock, patch

from app.agent import MoviePilotAgent
from app.agent.llm import AgentCapabilityManager, LLMHelper
from app.chain.message import MessageChain
from app.core.config import settings
from app.schemas.types import MessageChannel


def test_llm_supports_image_input_uses_model_catalog_text_only(monkeypatch):
    """内置目录明确为纯文本模型时，应自动关闭图片输入。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)

    assert not LLMHelper.supports_image_input(
        provider="minimax",
        model="MiniMax-M2.7",
    )


def test_llm_supports_image_input_keeps_known_vision_model(monkeypatch):
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


def test_handle_ai_message_routes_text_only_model_images_to_files(monkeypatch):
    """纯文本模型收到图片消息时，应降级为文件附件而非 image_url 内容块。"""
    chain = MessageChain()
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "minimax")
    monkeypatch.setattr(settings, "LLM_MODEL", "MiniMax-M2.7")

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
        "app.agent.agent_manager.process_message", new_callable=AsyncMock
    ) as process_message, patch(
        "app.chain.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: coro.close(),
    ):
        chain._handle_ai_message(
            text="/ai 帮我看看这张图",
            channel=MessageChannel.Telegram,
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
