import unittest
import sys
from unittest.mock import Mock, patch

sys.modules.setdefault("psutil", Mock())
sys.modules.setdefault("pyquery", Mock())

from app.core.config import settings
from app.helper.voice import VoiceHelper, OpenAIVoiceProvider


class VoiceHelperTest(unittest.TestCase):
    def test_registered_providers_contains_openai(self):
        self.assertIn("openai", VoiceHelper.get_registered_providers())

    def test_get_provider_falls_back_to_global_provider(self):
        with patch.object(settings, "AI_VOICE_PROVIDER", "openai"), patch.object(
            settings, "AI_VOICE_STT_PROVIDER", None
        ):
            provider = VoiceHelper.get_provider("stt")

        self.assertIsInstance(provider, OpenAIVoiceProvider)

    def test_is_available_checks_stt_and_tts_separately(self):
        provider = Mock()
        provider.is_available_for_stt.return_value = True
        provider.is_available_for_tts.return_value = False

        with patch.object(VoiceHelper, "get_provider", return_value=provider):
            self.assertTrue(VoiceHelper.is_available("stt"))
            self.assertFalse(VoiceHelper.is_available("tts"))

    def test_transcribe_bytes_routes_to_stt_provider(self):
        provider = Mock()
        provider.transcribe_bytes.return_value = "你好"

        with patch.object(VoiceHelper, "get_provider", return_value=provider):
            result = VoiceHelper.transcribe_bytes(b"audio")

        self.assertEqual(result, "你好")
        provider.transcribe_bytes.assert_called_once()

    def test_synthesize_speech_routes_to_tts_provider(self):
        provider = Mock()
        provider.synthesize_speech.return_value = "/tmp/reply.opus"

        with patch.object(VoiceHelper, "get_provider", return_value=provider):
            result = VoiceHelper.synthesize_speech("你好")

        self.assertEqual(result, "/tmp/reply.opus")
        provider.synthesize_speech.assert_called_once_with(text="你好")


if __name__ == "__main__":
    unittest.main()
