"""Agent 多模态能力 provider 与调度入口。"""

from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
from abc import ABC
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from app.runtime.config import settings
from app.runtime.log import logger
from app.adapters.network.http import RequestUtils


class AgentCapabilityProvider(ABC):
    """Agent 能力 provider 基类，后续图片等能力可继续扩展到这里。"""

    name: str


class AudioCapabilityProvider(AgentCapabilityProvider):
    """音频输入/输出能力 provider。"""

    MAX_TRANSCRIBE_BYTES = 10 * 1024 * 1024

    def is_available_for_audio_input(self) -> bool:
        """是否可用于音频输入转写。"""
        return False

    def is_available_for_audio_output(self) -> bool:
        """是否可用于语音合成输出。"""
        return False

    def transcribe_audio(self, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        """将音频字节转成文字。"""
        raise NotImplementedError

    def synthesize_speech(self, text: str) -> Optional[Path]:
        """将文字合成为可发送的音频文件。"""
        raise NotImplementedError


class OpenAIAudioProvider(AudioCapabilityProvider):
    """OpenAI / OpenAI-compatible 音频 provider。"""

    name = "openai"

    @staticmethod
    def _build_client(api_key: str, base_url: Optional[str]):
        from openai import OpenAI

        return OpenAI(api_key=api_key, base_url=base_url, max_retries=3)

    @staticmethod
    def _input_credentials() -> tuple[Optional[str], Optional[str]]:
        return settings.AUDIO_INPUT_API_KEY, settings.AUDIO_INPUT_BASE_URL

    @staticmethod
    def _output_credentials() -> tuple[Optional[str], Optional[str]]:
        return settings.AUDIO_OUTPUT_API_KEY, settings.AUDIO_OUTPUT_BASE_URL

    def is_available_for_audio_input(self) -> bool:
        api_key, _ = self._input_credentials()
        return bool(api_key)

    def is_available_for_audio_output(self) -> bool:
        api_key, _ = self._output_credentials()
        return bool(api_key)

    def transcribe_audio(self, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        if not content:
            return None
        if len(content) > self.MAX_TRANSCRIBE_BYTES:
            raise ValueError("语音文件超过 10MB，无法识别")

        try:
            api_key, base_url = self._input_credentials()
            if not api_key:
                raise ValueError("音频输入 provider 未配置 API Key")
            client = self._build_client(api_key=api_key, base_url=base_url)
            audio_file = BytesIO(content)
            audio_file.name = filename
            response = client.audio.transcriptions.create(
                model=settings.AUDIO_INPUT_MODEL,
                file=audio_file,
                language=settings.AUDIO_INPUT_LANGUAGE or "zh",
                response_format="verbose_json",
            )
            text = getattr(response, "text", None)
            return text.strip() if text else None
        except Exception as err:
            logger.error(f"音频输入转写失败: provider={self.name}, error={err}")
            return None

    def synthesize_speech(self, text: str) -> Optional[Path]:
        if not text:
            return None

        try:
            api_key, base_url = self._output_credentials()
            if not api_key:
                raise ValueError("音频输出 provider 未配置 API Key")
            client = self._build_client(api_key=api_key, base_url=base_url)
            voice_dir = settings.TEMP_PATH / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            output_path = voice_dir / f"{uuid4().hex}.opus"
            response = client.audio.speech.create(
                model=settings.AUDIO_OUTPUT_MODEL,
                voice=settings.AUDIO_OUTPUT_VOICE,
                input=text,
                response_format="opus",
            )
            response.write_to_file(output_path)
            return output_path
        except Exception as err:
            logger.error(f"音频输出合成失败: provider={self.name}, error={err}")
            return None


class OpenAIChatAudioProvider(AudioCapabilityProvider):
    """通过 OpenAI Chat Completions 兼容接口传入/返回音频的 provider。"""

    name = "openai_chat_audio"
    DISPLAY_NAME = "OpenAI Chat Audio"
    DEFAULT_BASE_URL: Optional[str] = None
    DEFAULT_STT_MODEL: Optional[str] = None
    DEFAULT_TTS_MODEL: Optional[str] = None
    DEFAULT_VOICE = "alloy"
    AUDIO_RESPONSE_FORMAT = "wav"
    AUDIO_INPUT_DATA_URL = False
    INCLUDE_AUDIO_MODALITIES = True
    TTS_MESSAGE_ROLE = "user"
    SUPPORTED_STT_MODELS: Optional[frozenset[str]] = None
    SUPPORTED_TTS_MODELS: Optional[frozenset[str]] = None
    UNSUPPORTED_TTS_MODELS = frozenset()
    SUPPORTED_AUDIO_MIME_TYPES = {
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
    }
    TRANSCODED_STT_SUFFIX = ".wav"
    TRANSCODED_STT_SAMPLE_RATE = "16000"

    def _build_client(self, api_key: str, base_url: Optional[str]):
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            max_retries=3,
        )

    @staticmethod
    def _input_credentials() -> tuple[Optional[str], Optional[str]]:
        return settings.AUDIO_INPUT_API_KEY, settings.AUDIO_INPUT_BASE_URL

    @staticmethod
    def _output_credentials() -> tuple[Optional[str], Optional[str]]:
        return settings.AUDIO_OUTPUT_API_KEY, settings.AUDIO_OUTPUT_BASE_URL

    def _normalize_stt_model(self) -> str:
        return self._normalize_model(
            model=settings.AUDIO_INPUT_MODEL,
            supported_models=self.SUPPORTED_STT_MODELS,
            default_model=self.DEFAULT_STT_MODEL,
        )

    def _normalize_tts_model(self) -> str:
        return self._normalize_model(
            model=settings.AUDIO_OUTPUT_MODEL,
            supported_models=self.SUPPORTED_TTS_MODELS,
            default_model=self.DEFAULT_TTS_MODEL,
        )

    @staticmethod
    def _normalize_model(
        model: Optional[str],
        supported_models: Optional[frozenset[str]],
        default_model: Optional[str],
    ) -> str:
        model = (model or "").strip()
        if not model:
            return default_model or ""
        if supported_models is None:
            return model
        model_key = model.lower()
        if model_key in supported_models:
            return model_key
        return default_model or model

    def _is_supported_tts_model(self) -> bool:
        model = self._normalize_tts_model()
        if not model:
            return False
        model_key = model.lower()
        if model_key in self.UNSUPPORTED_TTS_MODELS:
            return False
        return self.SUPPORTED_TTS_MODELS is None or model_key in self.SUPPORTED_TTS_MODELS

    @classmethod
    def _guess_audio_mime_type(cls, filename: str) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix in cls.SUPPORTED_AUDIO_MIME_TYPES:
            return cls.SUPPORTED_AUDIO_MIME_TYPES[suffix]
        mime_type, _ = mimetypes.guess_type(filename or "")
        return mime_type or "audio/ogg"

    @staticmethod
    def _guess_audio_format(filename: str) -> str:
        suffix = Path(filename or "").suffix.lower().lstrip(".")
        if suffix == "opus":
            return "ogg"
        return suffix or "ogg"

    def _build_audio_input_payload(self, content: bytes, filename: str) -> dict:
        """按不同 Chat Audio 兼容形态构造 input_audio 内容。"""
        audio_data = base64.b64encode(content).decode("utf-8")
        if self.AUDIO_INPUT_DATA_URL:
            mime_type = self._guess_audio_mime_type(filename)
            return {"data": f"data:{mime_type};base64,{audio_data}"}
        return {
            "data": audio_data,
            "format": self._guess_audio_format(filename),
        }

    def _normalize_audio_for_transcription(
        self, content: bytes, filename: str
    ) -> Optional[tuple[bytes, str]]:
        """
        将转写输入归一化为 Chat Audio provider 明确支持的格式。

        :param content: 原始音频字节
        :param filename: 原始音频文件名
        :return: 成功时返回可提交的音频字节和文件名，失败时返回 None
        """
        suffix = Path(filename or "").suffix.lower()
        if suffix in self.SUPPORTED_AUDIO_MIME_TYPES:
            return content, filename
        return self._convert_audio_for_transcription(content=content, filename=filename)

    def _convert_audio_for_transcription(
        self, content: bytes, filename: str
    ) -> Optional[tuple[bytes, str]]:
        """
        将 AMR 等第三方 STT 不支持的输入转为 WAV。

        :param content: 原始音频字节
        :param filename: 原始音频文件名
        :return: 成功时返回 WAV 字节和文件名，失败时返回 None
        """
        if not shutil.which("ffmpeg"):
            logger.warning(
                "%s STT 不支持当前音频格式且 ffmpeg 不可用，无法转码: filename=%s",
                self.DISPLAY_NAME,
                filename,
            )
            return None

        suffix = Path(filename or "").suffix.lower() or ".audio"
        voice_dir = settings.TEMP_PATH / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        input_path = voice_dir / f"{uuid4().hex}{suffix}"
        output_path = input_path.with_suffix(self.TRANSCODED_STT_SUFFIX)
        try:
            input_path.write_bytes(content)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-ar",
                self.TRANSCODED_STT_SAMPLE_RATE,
                "-ac",
                "1",
                "-f",
                "wav",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not output_path.exists():
                logger.warning(
                    "%s STT 音频转 WAV 失败: returncode=%s, stderr=%s",
                    self.DISPLAY_NAME,
                    result.returncode,
                    (result.stderr or "").strip()[:500],
                )
                return None
            return output_path.read_bytes(), f"{input_path.stem}{self.TRANSCODED_STT_SUFFIX}"
        finally:
            for temp_path in (input_path, output_path):
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as err:
                    logger.debug(f"清理 STT 临时音频失败: path={temp_path}, error={err}")

    @staticmethod
    def _extract_message_text(message) -> Optional[str]:
        """兼容音频理解响应可能放在 content 或 reasoning_content 的情况。"""
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()

        reasoning_content = getattr(message, "reasoning_content", None)
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            return reasoning_content.strip()

        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            for key in ("content", "reasoning_content"):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _extract_audio_data(message) -> Optional[str]:
        audio = getattr(message, "audio", None)
        if isinstance(audio, dict):
            return audio.get("data")
        if audio is not None:
            return getattr(audio, "data", None)

        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict) and isinstance(extra.get("audio"), dict):
            return extra["audio"].get("data")
        return None

    def _convert_wav_to_opus(self, wav_path: Path) -> Optional[Path]:
        """将 Chat Audio 返回的 WAV 转成 OGG/Opus，便于各通知渠道发送语音。"""
        if not shutil.which("ffmpeg"):
            return None

        output_path = wav_path.with_suffix(".opus")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(wav_path),
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output_path.exists():
            logger.warning(
                "%s TTS 音频转 Opus 失败，将使用 WAV 原文件: returncode=%s, stderr=%s",
                self.DISPLAY_NAME,
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return None
        return output_path

    def is_available_for_audio_input(self) -> bool:
        api_key, _ = self._input_credentials()
        return bool(api_key)

    def is_available_for_audio_output(self) -> bool:
        api_key, _ = self._output_credentials()
        return bool(api_key) and self._is_supported_tts_model()

    def transcribe_audio(self, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        if not content:
            return None
        if len(content) > self.MAX_TRANSCRIBE_BYTES:
            raise ValueError("语音文件超过 10MB，无法识别")

        try:
            api_key, base_url = self._input_credentials()
            if not api_key:
                raise ValueError("音频输入 provider 未配置 API Key")
            client = self._build_client(api_key=api_key, base_url=base_url)
            normalized_audio = self._normalize_audio_for_transcription(
                content=content, filename=filename
            )
            if not normalized_audio:
                return None
            content, filename = normalized_audio
            language = (settings.AUDIO_INPUT_LANGUAGE or "").strip()
            prompt = "请将这段音频完整转写为文字，只输出转写结果，不要添加解释。"
            if language:
                prompt += f"音频主要语言是 {language}。"

            completion = client.chat.completions.create(
                model=self._normalize_stt_model(),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": self._build_audio_input_payload(
                                    content=content, filename=filename
                                ),
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                max_completion_tokens=2048,
            )
            return self._extract_message_text(completion.choices[0].message)
        except Exception as err:
            logger.error(f"音频输入转写失败: provider={self.name}, error={err}")
            return None

    def synthesize_speech(self, text: str) -> Optional[Path]:
        if not text:
            return None
        if not self._is_supported_tts_model():
            logger.error(
                "%s TTS 当前不支持该模型或模型未配置: %s",
                self.DISPLAY_NAME,
                settings.AUDIO_OUTPUT_MODEL,
            )
            return None

        try:
            api_key, base_url = self._output_credentials()
            if not api_key:
                raise ValueError("音频输出 provider 未配置 API Key")
            client = self._build_client(api_key=api_key, base_url=base_url)
            voice_dir = settings.TEMP_PATH / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            wav_path = voice_dir / f"{uuid4().hex}.wav"
            request = {
                "model": self._normalize_tts_model(),
                "messages": [
                    {
                        "role": self.TTS_MESSAGE_ROLE,
                        "content": text,
                    }
                ],
                "audio": {
                    "format": self.AUDIO_RESPONSE_FORMAT,
                    "voice": settings.AUDIO_OUTPUT_VOICE or self.DEFAULT_VOICE,
                },
            }
            if self.INCLUDE_AUDIO_MODALITIES:
                request["modalities"] = ["text", "audio"]
            completion = client.chat.completions.create(**request)
            audio_data = self._extract_audio_data(completion.choices[0].message)
            if not audio_data:
                raise ValueError(f"{self.DISPLAY_NAME} TTS 响应中没有音频数据")

            wav_path.write_bytes(base64.b64decode(audio_data))
            return self._convert_wav_to_opus(wav_path) or wav_path
        except Exception as err:
            logger.error(f"音频输出合成失败: provider={self.name}, error={err}")
            return None


class MiMoAudioProvider(OpenAIChatAudioProvider):
    """Xiaomi MiMo Chat Audio 预设，仅接入普通 STT/TTS 能力。"""

    name = "mimo"
    DISPLAY_NAME = "Xiaomi MiMo"
    DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
    DEFAULT_STT_MODEL = "mimo-v2.5"
    DEFAULT_TTS_MODEL = "mimo-v2.5-tts"
    DEFAULT_VOICE = "mimo_default"
    AUDIO_INPUT_DATA_URL = True
    INCLUDE_AUDIO_MODALITIES = False
    TTS_MESSAGE_ROLE = "assistant"
    SUPPORTED_STT_MODELS = frozenset({"mimo-v2.5", "mimo-v2-omni"})
    SUPPORTED_TTS_MODELS = frozenset({DEFAULT_TTS_MODEL})
    UNSUPPORTED_TTS_MODELS = frozenset(
        {
            "mimo-v2.5-tts-voiceclone",
            "mimo-v2.5-tts-voicedesign",
        }
    )

    def _normalize_tts_model(self) -> str:
        model = (settings.AUDIO_OUTPUT_MODEL or "").strip().lower()
        if not model or not model.startswith("mimo-"):
            return self.DEFAULT_TTS_MODEL
        return model


class MiniMaxAudioProvider(OpenAIChatAudioProvider):
    """MiniMax 音频 provider，语音合成使用官方 T2A HTTP 接口。"""

    name = "minimax"
    DISPLAY_NAME = "MiniMax"
    DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
    DEFAULT_STT_MODEL = "MiniMax-M2.7"
    DEFAULT_TTS_MODEL = "speech-2.8-turbo"
    DEFAULT_VOICE = "Chinese (Mandarin)_Lyrical_Voice"
    AUDIO_INPUT_DATA_URL = True
    SUPPORTED_TTS_MODELS = frozenset(
        {
            "speech-2.8-hd",
            "speech-2.8-turbo",
            "speech-2.6-hd",
            "speech-2.6-turbo",
            "speech-02-hd",
            "speech-02-turbo",
            "speech-01-hd",
            "speech-01-turbo",
        }
    )

    def _build_client(self, api_key: str, base_url: Optional[str]):
        """构建 MiniMax OpenAI 兼容客户端，兼容用户误填 Anthropic 端点的情况。"""
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            base_url=self._normalize_api_base_url(base_url),
            max_retries=3,
        )

    @classmethod
    def _normalize_api_base_url(cls, base_url: Optional[str]) -> str:
        """归一化 MiniMax API 基础 URL，确保后续可以拼接 OpenAI/T2A 路径。"""
        normalized = (base_url or cls.DEFAULT_BASE_URL).strip().rstrip("/")
        if normalized.endswith("/t2a_v2"):
            normalized = normalized[: -len("/t2a_v2")]
        for suffix in ("/anthropic/v1", "/openai/v1"):
            if normalized.endswith(suffix):
                return normalized[: -len(suffix)] + "/v1"
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        return normalized

    @classmethod
    def _build_t2a_url(cls, base_url: Optional[str]) -> str:
        """生成 MiniMax 同步 T2A 接口地址。"""
        return f"{cls._normalize_api_base_url(base_url)}/t2a_v2"

    def _normalize_stt_model(self) -> str:
        """将非 MiniMax 的默认转写模型名兜底为 MiniMax 对话模型。"""
        model = (settings.AUDIO_INPUT_MODEL or "").strip()
        if not model or model.lower().startswith(("gpt-", "mimo-")):
            return self.DEFAULT_STT_MODEL
        return model

    def _normalize_tts_model(self) -> str:
        """将非 MiniMax 语音模型兜底为官方 T2A 模型。"""
        model = (settings.AUDIO_OUTPUT_MODEL or "").strip().lower()
        if model in self.SUPPORTED_TTS_MODELS:
            return model
        return self.DEFAULT_TTS_MODEL

    def _normalize_voice_id(self) -> str:
        """将其他 provider 的默认音色兜底为 MiniMax 中文系统音色。"""
        voice_id = (settings.AUDIO_OUTPUT_VOICE or "").strip()
        if not voice_id or voice_id in {"alloy", "mimo_default"}:
            return self.DEFAULT_VOICE
        return voice_id

    @staticmethod
    def _decode_audio_payload(audio_data: str) -> bytes:
        """解析 MiniMax T2A 返回的音频数据，优先按官方 hex 格式处理。"""
        normalized = "".join((audio_data or "").split())
        try:
            return bytes.fromhex(normalized)
        except ValueError:
            return base64.b64decode(audio_data)

    @staticmethod
    def _extract_minimax_error(data: dict[str, Any]) -> Optional[str]:
        """提取 MiniMax base_resp 错误信息，成功响应返回 None。"""
        base_resp = data.get("base_resp") or {}
        status_code = base_resp.get("status_code")
        if status_code in (None, 0, "0"):
            return None
        status_msg = base_resp.get("status_msg") or "unknown error"
        return f"{status_code}: {status_msg}"

    def synthesize_speech(self, text: str) -> Optional[Path]:
        """调用 MiniMax T2A HTTP 接口合成语音文件。"""
        if not text:
            return None

        try:
            api_key, base_url = self._output_credentials()
            if not api_key:
                raise ValueError("音频输出 provider 未配置 API Key")
            response = RequestUtils(
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                proxies=settings.PROXY or {},
                timeout=60,
            ).post_res(
                url=self._build_t2a_url(base_url),
                json={
                    "model": self._normalize_tts_model(),
                    "text": text,
                    "stream": False,
                    "language_boost": "auto",
                    "output_format": "hex",
                    "voice_setting": {
                        "voice_id": self._normalize_voice_id(),
                        "speed": 1,
                        "vol": 1,
                        "pitch": 0,
                    },
                    "audio_setting": {
                        "sample_rate": 32000,
                        "bitrate": 128000,
                        "format": "opus",
                        "channel": 1,
                    },
                },
            )
            if not response:
                raise ValueError("MiniMax T2A 请求无响应")
            if response.status_code >= 400:
                raise ValueError(f"MiniMax T2A HTTP {response.status_code}")

            result = response.json()
            minimax_error = self._extract_minimax_error(result)
            if minimax_error:
                raise ValueError(f"MiniMax T2A 返回错误: {minimax_error}")

            audio_data = ((result.get("data") or {}).get("audio") or "").strip()
            if not audio_data:
                raise ValueError("MiniMax T2A 响应中没有音频数据")

            voice_dir = settings.TEMP_PATH / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            output_path = voice_dir / f"{uuid4().hex}.opus"
            output_path.write_bytes(self._decode_audio_payload(audio_data))
            return output_path
        except Exception as err:
            logger.error(f"音频输出合成失败: provider={self.name}, error={err}")
            return None


class AgentCapabilityManager:
    """Agent 能力统一入口。"""

    REPLY_MODE_NATIVE = "native_voice"
    REPLY_MODE_TEXT = "text"
    _audio_providers: Dict[str, AudioCapabilityProvider] = {
        OpenAIAudioProvider.name: OpenAIAudioProvider(),
        OpenAIChatAudioProvider.name: OpenAIChatAudioProvider(),
        MiMoAudioProvider.name: MiMoAudioProvider(),
        MiniMaxAudioProvider.name: MiniMaxAudioProvider(),
    }

    @classmethod
    def register_audio_provider(cls, provider: AudioCapabilityProvider) -> None:
        """注册新的音频 provider。"""
        cls._audio_providers[provider.name.lower()] = provider

    @classmethod
    def get_registered_audio_providers(cls) -> list[str]:
        """返回已注册的音频 provider 名称。"""
        return sorted(cls._audio_providers.keys())

    @staticmethod
    def _normalize_provider_name(provider: Optional[str]) -> str:
        return (provider or "openai").strip().lower()

    @staticmethod
    def _get_provider_log_name(provider: AudioCapabilityProvider) -> str:
        provider_name = getattr(provider, "name", None)
        return provider_name if isinstance(provider_name, str) else provider.__class__.__name__

    @classmethod
    def get_audio_provider(cls, mode: str) -> Optional[AudioCapabilityProvider]:
        provider_name = cls._normalize_provider_name(
            settings.AUDIO_INPUT_PROVIDER
            if (mode or "").lower() == "input"
            else settings.AUDIO_OUTPUT_PROVIDER
        )
        provider = cls._audio_providers.get(provider_name)
        if provider:
            return provider
        logger.warning("未注册音频 provider: mode=%s, provider=%s", mode, provider_name)
        return None

    @staticmethod
    def supports_image_input() -> bool:
        """当前 Agent 是否启用图片输入能力。"""
        from app.agent.llm.helper import LLMHelper

        return LLMHelper.supports_image_input()

    @staticmethod
    def supports_audio_input() -> bool:
        """当前 Agent 是否启用音频输入能力。"""
        return bool(settings.LLM_SUPPORT_AUDIO_INPUT)

    @staticmethod
    def supports_audio_output() -> bool:
        """当前 Agent 是否启用音频输出能力。"""
        return bool(settings.LLM_SUPPORT_AUDIO_OUTPUT)

    @classmethod
    def is_audio_input_available(cls) -> bool:
        if not cls.supports_audio_input():
            return False
        provider = cls.get_audio_provider("input")
        return bool(provider and provider.is_available_for_audio_input())

    @classmethod
    def is_audio_output_available(cls) -> bool:
        if not cls.supports_audio_output():
            return False
        provider = cls.get_audio_provider("output")
        return bool(provider and provider.is_available_for_audio_output())

    @classmethod
    def transcribe_audio(cls, content: bytes, filename: str = "input.ogg") -> Optional[str]:
        """将语音文件内容转写为文字，并记录能力调用日志。"""
        provider = cls.get_audio_provider("input")
        if not provider or not cls.is_audio_input_available():
            logger.info("语音转文字跳过：音频输入能力未启用或 provider 不可用")
            return None
        provider_name = cls._get_provider_log_name(provider)
        logger.info(
            f"语音转文字开始：provider={provider_name}, filename={filename}, "
            f"bytes={len(content) if content else 0}"
        )
        transcript = provider.transcribe_audio(content=content, filename=filename)
        if transcript:
            logger.info(
                f"语音转文字完成：provider={provider_name}, filename={filename}, "
                f"text_len={len(transcript)}"
            )
        else:
            logger.info(
                f"语音转文字无结果：provider={provider_name}, filename={filename}"
            )
        return transcript

    @classmethod
    def synthesize_speech(cls, text: str) -> Optional[Path]:
        """将文字合成为语音文件，并记录能力调用日志。"""
        provider = cls.get_audio_provider("output")
        if not provider or not cls.is_audio_output_available():
            logger.info("文字转语音跳过：音频输出能力未启用或 provider 不可用")
            return None
        provider_name = cls._get_provider_log_name(provider)
        logger.info(
            f"文字转语音开始：provider={provider_name}, text_len={len(text) if text else 0}"
        )
        output_path = provider.synthesize_speech(text=text)
        if output_path:
            logger.info(f"文字转语音完成：provider={provider_name}, path={output_path}")
        else:
            logger.info(f"文字转语音无结果：provider={provider_name}")
        return output_path

    @classmethod
    def resolve_reply_mode(cls, channel: Optional[str], source: Optional[str]) -> str:
        """仅在支持原生语音回复的渠道上发送音频，其余渠道回退文字。"""
        if cls.supports_native_voice_reply(channel=channel, source=source):
            return cls.REPLY_MODE_NATIVE
        return cls.REPLY_MODE_TEXT

    @classmethod
    def _parse_message_channel(cls, channel: Optional[Any]):
        """将渠道入参归一化为内建渠道枚举，非内建取值保留为渠道标识。

        :param channel: 渠道枚举、枚举取值、枚举成员名、带枚举类名前缀的写法或扩展渠道标识
        :return: 命中内建渠道时为枚举成员，否则为渠道标识字符串；空取值为 ``None``
        """
        from app.schemas.notification import resolve_channel
        from app.schemas.types import NotificationChannel

        resolved = resolve_channel(channel)
        if not isinstance(resolved, str):
            return resolved

        lowered_channel = resolved.lower()
        for channel_item in NotificationChannel:
            aliases = {
                channel_item.value.lower(),
                channel_item.name.lower(),
                f"{NotificationChannel.__name__}.{channel_item.name}".lower(),
            }
            if lowered_channel in aliases:
                return channel_item
        return resolved

    @staticmethod
    def _is_wechat_app_mode(source: Optional[str]) -> bool:
        """判断企业微信来源是否为自建应用模式。"""
        if not source:
            return False

        from app.runtime.extensions.service_config import ServiceConfigHelper

        for config in ServiceConfigHelper.get_notification_configs():
            if config.name != source:
                continue
            return (config.config or {}).get("WECHAT_MODE", "app") != "bot"
        return False

    @classmethod
    def supports_native_voice_reply(
            cls, channel: Optional[str], source: Optional[str]
    ) -> bool:
        """判断当前渠道是否支持原生语音消息发送。"""
        from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
        from app.schemas.types import NotificationChannel

        channel_enum = cls._parse_message_channel(channel)
        if not channel_enum:
            return False

        if not ChannelCapabilityManager.supports_capability(
                channel_enum, ChannelCapability.AUDIO_OUTPUT
        ):
            return False

        if channel_enum == NotificationChannel.Wechat:
            return cls._is_wechat_app_mode(source)
        return True
