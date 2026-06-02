import unittest
from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import settings
from app.schemas.message import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageChannel

from app.agent.llm import capability as capability_module
from app.agent.llm.capability import (
    AgentCapabilityManager,
    MiMoAudioProvider,
    MiniMaxAudioProvider,
    OpenAIChatAudioProvider,
    OpenAIAudioProvider,
)


class AgentCapabilityManagerTest(unittest.TestCase):
    def test_registered_audio_providers_contains_builtin_providers(self):
        self.assertIn("openai", AgentCapabilityManager.get_registered_audio_providers())
        self.assertIn(
            "openai_chat_audio", AgentCapabilityManager.get_registered_audio_providers()
        )
        self.assertIn("mimo", AgentCapabilityManager.get_registered_audio_providers())
        self.assertIn("minimax", AgentCapabilityManager.get_registered_audio_providers())

    def test_get_audio_provider_uses_separate_input_and_output_settings(self):
        with patch.object(settings, "AUDIO_INPUT_PROVIDER", "openai"), patch.object(
            settings, "AUDIO_OUTPUT_PROVIDER", "mimo"
        ):
            self.assertIsInstance(
                AgentCapabilityManager.get_audio_provider("input"), OpenAIAudioProvider
            )
            self.assertIsInstance(
                AgentCapabilityManager.get_audio_provider("output"), MiMoAudioProvider
            )

    def test_chat_audio_provider_keeps_arbitrary_compatible_models(self):
        provider = OpenAIChatAudioProvider()

        with patch.object(
            settings, "AUDIO_INPUT_MODEL", "vendor-omni-audio"
        ), patch.object(settings, "AUDIO_OUTPUT_MODEL", "vendor-tts-audio"):
            self.assertEqual(provider._normalize_stt_model(), "vendor-omni-audio")
            self.assertEqual(provider._normalize_tts_model(), "vendor-tts-audio")

    def test_chat_audio_provider_uses_openai_audio_payload_shape(self):
        provider = OpenAIChatAudioProvider()
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))]
        )

        with patch.object(provider, "_build_client", return_value=fake_client), patch.object(
            settings, "AUDIO_INPUT_MODEL", "gpt-4o-audio-preview"
        ), patch.object(settings, "AUDIO_INPUT_LANGUAGE", "zh"), patch.object(
            settings, "AUDIO_INPUT_API_KEY", "sk-test"
        ), patch.object(settings, "AUDIO_INPUT_BASE_URL", "https://example.com/v1"):
            result = provider.transcribe_audio(b"audio-bytes", filename="input.wav")

        self.assertEqual(result, "你好")
        request = fake_client.chat.completions.create.call_args.kwargs
        content = request["messages"][0]["content"]
        self.assertEqual(
            content[0]["input_audio"],
            {"data": b64encode(b"audio-bytes").decode("utf-8"), "format": "wav"},
        )

    def test_chat_audio_provider_requests_audio_modality_for_tts(self):
        provider = OpenAIChatAudioProvider()
        fake_client = Mock()
        audio_data = b64encode(b"wav-bytes").decode("utf-8")
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(audio={"data": audio_data}))]
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            provider, "_build_client", return_value=fake_client
        ), patch.object(
            capability_module,
            "settings",
            SimpleNamespace(
                TEMP_PATH=Path(temp_dir),
                AUDIO_OUTPUT_MODEL="gpt-4o-audio-preview",
                AUDIO_OUTPUT_VOICE="alloy",
                AUDIO_OUTPUT_API_KEY="sk-test",
                AUDIO_OUTPUT_BASE_URL="https://example.com/v1",
            ),
        ), patch.object(provider, "_convert_wav_to_opus", return_value=None):
            output_path = provider.synthesize_speech("你好")

        self.assertIsNotNone(output_path)
        request = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["messages"][0]["role"], "user")
        self.assertEqual(request["modalities"], ["text", "audio"])
        self.assertEqual(request["audio"], {"format": "wav", "voice": "alloy"})

    def test_audio_input_and_output_switches_are_independent(self):
        provider = Mock()
        provider.is_available_for_audio_input.return_value = True
        provider.is_available_for_audio_output.return_value = True

        with patch.object(
            settings, "LLM_SUPPORT_AUDIO_INPUT", True
        ), patch.object(
            settings, "LLM_SUPPORT_AUDIO_OUTPUT", False
        ), patch.object(
            AgentCapabilityManager, "get_audio_provider", return_value=provider
        ):
            self.assertTrue(AgentCapabilityManager.is_audio_input_available())
            self.assertFalse(AgentCapabilityManager.is_audio_output_available())

        with patch.object(
            settings, "LLM_SUPPORT_AUDIO_INPUT", False
        ), patch.object(
            settings, "LLM_SUPPORT_AUDIO_OUTPUT", True
        ), patch.object(
            AgentCapabilityManager, "get_audio_provider", return_value=provider
        ):
            self.assertFalse(AgentCapabilityManager.is_audio_input_available())
            self.assertTrue(AgentCapabilityManager.is_audio_output_available())

    def test_transcribe_audio_routes_to_input_provider(self):
        provider = Mock()
        provider.name = "mock_audio"
        provider.is_available_for_audio_input.return_value = True
        provider.transcribe_audio.return_value = "你好"

        with patch.object(settings, "LLM_SUPPORT_AUDIO_INPUT", True), patch.object(
            AgentCapabilityManager, "get_audio_provider", return_value=provider
        ), patch.object(capability_module.logger, "info") as log_info:
            result = AgentCapabilityManager.transcribe_audio(b"audio")

        self.assertEqual(result, "你好")
        provider.transcribe_audio.assert_called_once()
        self.assertTrue(
            any("语音转文字开始" in call.args[0] for call in log_info.call_args_list)
        )
        self.assertTrue(
            any("语音转文字完成" in call.args[0] for call in log_info.call_args_list)
        )

    def test_synthesize_speech_routes_to_output_provider(self):
        provider = Mock()
        provider.name = "mock_audio"
        provider.is_available_for_audio_output.return_value = True
        provider.synthesize_speech.return_value = Path("/tmp/reply.opus")

        with patch.object(settings, "LLM_SUPPORT_AUDIO_OUTPUT", True), patch.object(
            AgentCapabilityManager, "get_audio_provider", return_value=provider
        ), patch.object(capability_module.logger, "info") as log_info:
            result = AgentCapabilityManager.synthesize_speech("你好")

        self.assertEqual(result, Path("/tmp/reply.opus"))
        provider.synthesize_speech.assert_called_once_with(text="你好")
        self.assertTrue(
            any("文字转语音开始" in call.args[0] for call in log_info.call_args_list)
        )
        self.assertTrue(
            any("文字转语音完成" in call.args[0] for call in log_info.call_args_list)
        )

    def test_native_voice_reply_supports_channels_with_audio_output(self):
        """校验 Agent 语音回复渠道支持判断覆盖常见渠道写法。"""
        self.assertTrue(
            AgentCapabilityManager.supports_native_voice_reply("telegram", None)
        )
        self.assertTrue(
            AgentCapabilityManager.supports_native_voice_reply(
                MessageChannel.Telegram.value, None
            )
        )
        self.assertTrue(
            AgentCapabilityManager.supports_native_voice_reply(
                MessageChannel.Feishu.value, None
            )
        )
        self.assertTrue(
            AgentCapabilityManager.supports_native_voice_reply("Feishu", None)
        )
        self.assertFalse(
            AgentCapabilityManager.supports_native_voice_reply("Slack", None)
        )

    def test_native_voice_reply_respects_wechat_mode(self):
        """校验企业微信只有自建应用模式允许 Agent 语音回复。"""
        configs = [
            SimpleNamespace(name="wechat-app", config={"WECHAT_MODE": "app"}),
            SimpleNamespace(name="wechat-bot", config={"WECHAT_MODE": "bot"}),
        ]

        with patch(
            "app.helper.service.ServiceConfigHelper.get_notification_configs",
            return_value=configs,
        ):
            self.assertTrue(
                AgentCapabilityManager.supports_native_voice_reply(
                    MessageChannel.Wechat.value, "wechat-app"
                )
            )
            self.assertFalse(
                AgentCapabilityManager.supports_native_voice_reply(
                    MessageChannel.Wechat.value, "wechat-bot"
                )
            )
            self.assertFalse(
                AgentCapabilityManager.supports_native_voice_reply(
                    MessageChannel.Wechat.value, "missing"
                )
            )

    def test_channel_capability_marks_voice_output_channels(self):
        """校验消息渠道能力显式声明原生语音输出支持。"""
        for channel in (
            MessageChannel.Telegram,
            MessageChannel.Feishu,
            MessageChannel.Wechat,
        ):
            self.assertTrue(
                ChannelCapabilityManager.supports_capability(
                    channel, ChannelCapability.AUDIO_OUTPUT
                )
            )
        self.assertFalse(
            ChannelCapabilityManager.supports_capability(
                MessageChannel.Slack, ChannelCapability.AUDIO_OUTPUT
            )
        )

    def test_mimo_tts_uses_chat_completions_audio_payload(self):
        provider = MiMoAudioProvider()
        fake_client = Mock()
        audio_data = b64encode(b"wav-bytes").decode("utf-8")
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(audio={"data": audio_data}))]
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            provider, "_build_client", return_value=fake_client
        ), patch.object(
            capability_module,
            "settings",
            SimpleNamespace(
                TEMP_PATH=Path(temp_dir),
                AUDIO_OUTPUT_MODEL="mimo-v2.5-tts",
                AUDIO_OUTPUT_VOICE="冰糖",
                AUDIO_OUTPUT_API_KEY="sk-test",
                AUDIO_OUTPUT_BASE_URL="https://api.xiaomimimo.com/v1",
            ),
        ), patch.object(provider, "_convert_wav_to_opus", return_value=None):
            output_path = provider.synthesize_speech("你好")
            output_bytes = output_path.read_bytes() if output_path else None

        self.assertIsNotNone(output_path)
        self.assertEqual(output_bytes, b"wav-bytes")
        fake_client.chat.completions.create.assert_called_once()
        request = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "mimo-v2.5-tts")
        self.assertEqual(request["messages"][0]["role"], "assistant")
        self.assertEqual(request["messages"][0]["content"], "你好")
        self.assertEqual(request["audio"], {"format": "wav", "voice": "冰糖"})

    def test_mimo_tts_rejects_voice_design_and_clone_models(self):
        provider = MiMoAudioProvider()

        with patch.object(
            settings, "AUDIO_OUTPUT_MODEL", "mimo-v2.5-tts-voiceclone"
        ), patch.object(provider, "_build_client") as build_client:
            result = provider.synthesize_speech("你好")

        self.assertIsNone(result)
        build_client.assert_not_called()

    def test_mimo_stt_rejects_non_audio_mimo_models_by_falling_back(self):
        provider = MiMoAudioProvider()

        with patch.object(settings, "AUDIO_INPUT_MODEL", "mimo-v2.5-pro"):
            self.assertEqual(provider._normalize_stt_model(), "mimo-v2.5")

    def test_mimo_stt_uses_base64_audio_input(self):
        provider = MiMoAudioProvider()
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))]
        )

        with patch.object(provider, "_build_client", return_value=fake_client), patch.object(
            settings, "AUDIO_INPUT_MODEL", "mimo-v2.5"
        ), patch.object(settings, "AUDIO_INPUT_LANGUAGE", "zh"), patch.object(
            settings, "AUDIO_INPUT_API_KEY", "sk-test"
        ), patch.object(
            settings, "AUDIO_INPUT_BASE_URL", "https://api.xiaomimimo.com/v1"
        ):
            result = provider.transcribe_audio(b"audio-bytes", filename="input.wav")

        self.assertEqual(result, "你好")
        request = fake_client.chat.completions.create.call_args.kwargs
        content = request["messages"][0]["content"]
        self.assertEqual(request["model"], "mimo-v2.5")
        self.assertTrue(
            content[0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
        )
        self.assertIn("只输出转写结果", content[1]["text"])

    def test_mimo_stt_transcodes_amr_before_payload(self):
        """校验 MiMo 音频输入会先将企业微信 AMR 转为受支持的 WAV。"""
        provider = MiMoAudioProvider()
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))]
        )

        with patch.object(provider, "_build_client", return_value=fake_client), patch.object(
            provider,
            "_convert_audio_for_transcription",
            return_value=(b"wav-bytes", "input.wav"),
        ) as convert_audio, patch.object(settings, "AUDIO_INPUT_MODEL", "mimo-v2.5"), patch.object(
            settings, "AUDIO_INPUT_LANGUAGE", "zh"
        ), patch.object(
            settings, "AUDIO_INPUT_API_KEY", "sk-test"
        ), patch.object(
            settings, "AUDIO_INPUT_BASE_URL", "https://api.xiaomimimo.com/v1"
        ):
            result = provider.transcribe_audio(b"amr-bytes", filename="input.amr")

        self.assertEqual(result, "你好")
        convert_audio.assert_called_once_with(
            content=b"amr-bytes", filename="input.amr"
        )
        request = fake_client.chat.completions.create.call_args.kwargs
        content = request["messages"][0]["content"]
        self.assertEqual(
            content[0]["input_audio"]["data"],
            f"data:audio/wav;base64,{b64encode(b'wav-bytes').decode('utf-8')}",
        )

    def test_minimax_stt_normalizes_openai_default_model(self):
        """校验 MiniMax 音频输入会把 OpenAI 默认模型兜底为 MiniMax 模型。"""
        provider = MiniMaxAudioProvider()

        with patch.object(settings, "AUDIO_INPUT_MODEL", "gpt-4o-mini-transcribe"):
            self.assertEqual(provider._normalize_stt_model(), "MiniMax-M2.7")

    def test_minimax_tts_uses_t2a_http_payload(self):
        """校验 MiniMax 音频输出会调用官方 T2A HTTP 接口并写入音频文件。"""
        provider = MiniMaxAudioProvider()
        fake_response = SimpleNamespace(
            status_code=200,
            json=Mock(
                return_value={
                    "data": {"audio": b"opus-bytes".hex(), "status": 2},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }
            ),
        )
        request_utils = Mock()
        request_utils.post_res.return_value = fake_response

        with TemporaryDirectory() as temp_dir, patch.object(
            capability_module, "RequestUtils", return_value=request_utils
        ) as request_utils_cls, patch.object(
            capability_module,
            "settings",
            SimpleNamespace(
                TEMP_PATH=Path(temp_dir),
                PROXY={},
                AUDIO_OUTPUT_MODEL="gpt-4o-mini-tts",
                AUDIO_OUTPUT_VOICE="alloy",
                AUDIO_OUTPUT_API_KEY="sk-test",
                AUDIO_OUTPUT_BASE_URL="https://api.minimaxi.com/anthropic/v1",
            ),
        ):
            output_path = provider.synthesize_speech("你好")
            output_bytes = output_path.read_bytes() if output_path else None

        self.assertIsNotNone(output_path)
        self.assertEqual(output_bytes, b"opus-bytes")
        request_utils_cls.assert_called_once()
        request = request_utils.post_res.call_args.kwargs
        self.assertEqual(request["url"], "https://api.minimaxi.com/v1/t2a_v2")
        self.assertEqual(request["json"]["model"], "speech-2.8-turbo")
        self.assertEqual(
            request["json"]["voice_setting"]["voice_id"],
            "Chinese (Mandarin)_Lyrical_Voice",
        )
        self.assertEqual(request["json"]["audio_setting"]["format"], "opus")

    def test_minimax_tts_accepts_base64_audio_payload(self):
        """校验 MiniMax 音频解析兼容部分代理返回的 base64 音频数据。"""
        provider = MiniMaxAudioProvider()

        self.assertEqual(
            provider._decode_audio_payload(b64encode(b"opus-bytes").decode("utf-8")),
            b"opus-bytes",
        )
