"""语音能力辅助功能。"""

from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from app.core.config import settings
from app.log import logger


class VoiceProvider(ABC):
    """语音 provider 抽象层。"""

    MAX_TRANSCRIBE_BYTES = 25 * 1024 * 1024

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称。"""

    @abstractmethod
    def is_available_for_stt(self) -> bool:
        """是否可用于语音识别。"""

    @abstractmethod
    def is_available_for_tts(self) -> bool:
        """是否可用于语音合成。"""

    @abstractmethod
    def transcribe_bytes(self, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        """将音频字节转成文字。"""

    @abstractmethod
    def synthesize_speech(self, text: str) -> Optional[Path]:
        """将文字转成语音文件。"""


class OpenAIVoiceProvider(VoiceProvider):
    """OpenAI / OpenAI-compatible provider。"""

    @property
    def name(self) -> str:
        return "openai"

    @staticmethod
    def _resolve_credentials(mode: str) -> tuple[Optional[str], Optional[str]]:
        mode = mode.lower()
        provider = (
            settings.AI_VOICE_STT_PROVIDER
            if mode == "stt"
            else settings.AI_VOICE_TTS_PROVIDER
        ) or settings.AI_VOICE_PROVIDER
        provider = (provider or "").strip().lower()

        api_key = (
            settings.AI_VOICE_STT_API_KEY
            if mode == "stt"
            else settings.AI_VOICE_TTS_API_KEY
        ) or settings.AI_VOICE_API_KEY
        base_url = (
            settings.AI_VOICE_STT_BASE_URL
            if mode == "stt"
            else settings.AI_VOICE_TTS_BASE_URL
        ) or settings.AI_VOICE_BASE_URL

        if (
            not api_key
            and provider == "openai"
            and (settings.LLM_PROVIDER or "").strip().lower() == "openai"
        ):
            api_key = settings.LLM_API_KEY
            base_url = base_url or settings.LLM_BASE_URL

        return api_key, base_url

    def _get_client(self, mode: str):
        from openai import OpenAI

        api_key, base_url = self._resolve_credentials(mode)
        if not api_key:
            raise ValueError(f"{mode.upper()} provider 未配置 API Key")
        return OpenAI(api_key=api_key, base_url=base_url, max_retries=3)

    def is_available_for_stt(self) -> bool:
        api_key, _ = self._resolve_credentials("stt")
        return bool(api_key)

    def is_available_for_tts(self) -> bool:
        api_key, _ = self._resolve_credentials("tts")
        return bool(api_key)

    def transcribe_bytes(self, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        if not content:
            return None
        if len(content) > self.MAX_TRANSCRIBE_BYTES:
            raise ValueError("语音文件超过 25MB，无法识别")

        try:
            client = self._get_client("stt")
            audio_file = BytesIO(content)
            audio_file.name = filename
            response = client.audio.transcriptions.create(
                model=settings.AI_VOICE_STT_MODEL,
                file=audio_file,
                language=settings.AI_VOICE_LANGUAGE or "zh",
                response_format="verbose_json",
            )
            text = getattr(response, "text", None)
            return text.strip() if text else None
        except Exception as err:
            logger.error(f"语音转文字失败: provider={self.name}, error={err}")
            return None

    def synthesize_speech(self, text: str) -> Optional[Path]:
        if not text:
            return None

        try:
            client = self._get_client("tts")
            voice_dir = settings.TEMP_PATH / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            output_path = voice_dir / f"{uuid4().hex}.opus"
            response = client.audio.speech.create(
                model=settings.AI_VOICE_TTS_MODEL,
                voice=settings.AI_VOICE_TTS_VOICE,
                input=text,
                response_format="opus",
            )
            response.write_to_file(output_path)
            return output_path
        except Exception as err:
            logger.error(f"文字转语音失败: provider={self.name}, error={err}")
            return None


class VoiceHelper:
    """统一语音入口，负责按 STT/TTS provider 路由。"""

    _providers: Dict[str, VoiceProvider] = {
        "openai": OpenAIVoiceProvider(),
    }

    @classmethod
    def register_provider(cls, provider: VoiceProvider) -> None:
        cls._providers[provider.name.lower()] = provider

    @staticmethod
    def _resolve_provider_name(mode: str) -> str:
        mode = mode.lower()
        provider = (
            settings.AI_VOICE_STT_PROVIDER
            if mode == "stt"
            else settings.AI_VOICE_TTS_PROVIDER
        ) or settings.AI_VOICE_PROVIDER
        return (provider or "openai").strip().lower()

    @classmethod
    def get_provider(cls, mode: str) -> Optional[VoiceProvider]:
        provider_name = cls._resolve_provider_name(mode)
        provider = cls._providers.get(provider_name)
        if provider:
            return provider
        logger.warning(f"未注册语音 provider: mode={mode}, provider={provider_name}")
        return None

    @classmethod
    def get_registered_providers(cls) -> list[str]:
        return sorted(cls._providers.keys())

    @classmethod
    def is_available(cls, mode: Optional[str] = None) -> bool:
        if mode:
            provider = cls.get_provider(mode)
            if not provider:
                return False
            return (
                provider.is_available_for_stt()
                if mode.lower() == "stt"
                else provider.is_available_for_tts()
            )
        return cls.is_available("stt") or cls.is_available("tts")

    @classmethod
    def transcribe_bytes(cls, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        provider = cls.get_provider("stt")
        if not provider:
            return None
        return provider.transcribe_bytes(content=content, filename=filename)

    @classmethod
    def synthesize_speech(cls, text: str) -> Optional[Path]:
        provider = cls.get_provider("tts")
        if not provider:
            return None
        return provider.synthesize_speech(text=text)
