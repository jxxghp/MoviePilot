import time
from typing import Any, Optional, Tuple, Union

from app.adapters.network.http import RequestUtils
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.modules import _ModuleBase
from app.runtime.log import logger
from app.runtime.settings import RuntimeSettingsCompat
from app.schemas.types import ModuleType, OtherModulesType

settings = RuntimeSettingsCompat()


class MusixmatchModule(_ModuleBase):
    """使用用户授权的 Musixmatch 官方 API 获取同步或纯文本歌词。"""

    _source = "musixmatch"
    _cooldown_until = 0.0

    def init_module(self) -> None:
        """初始化无持久状态的授权歌词模块。"""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """仅在配置官方 API Key 后启用模块。"""
        return "MUSIXMATCH_API_KEY", True

    def stop(self) -> None:
        """停止模块；当前没有需要释放的资源。"""

    def test(self) -> Tuple[bool, str]:
        """验证 API Key 和官方接口连通性。"""
        if not str(settings.MUSIXMATCH_API_KEY or "").strip():
            return False, "Musixmatch API Key 未配置"
        payload = self._request("matcher.lyrics.get", {"q_track": "test", "q_artist": "test"})
        return (True, "") if payload is not None else (False, "Musixmatch API 连接或授权失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "Musixmatch"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属类型。"""
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """返回 Musixmatch 模块子类型。"""
        return OtherModulesType.Musixmatch

    @staticmethod
    def get_priority() -> int:
        """授权同步歌词优先于免费来源参与候选评分。"""
        return 4

    def music_lyrics_candidates(
            self,
            music: Union[MetaMusic, MusicInfo],
    ) -> list[MusicLyrics]:
        """按标题、艺术家和时长调用官方 matcher 接口。"""
        title = str(getattr(music, "title", None) or "").strip()
        artists = list(getattr(music, "artists", None) or [])
        artist = str((artists[0] if artists else getattr(music, "album_artist", None)) or "").strip()
        if not title or not artist:
            return []
        params: dict[str, Any] = {"q_track": title, "q_artist": artist}
        duration = self._optional_int(getattr(music, "duration", None))
        if duration:
            params.update({
                "f_subtitle_length": duration,
                "f_subtitle_length_max_deviation": 2,
            })
        subtitle = self._response_item(self._request("matcher.subtitle.get", params), "subtitle")
        if subtitle and not subtitle.get("restricted"):
            body = str(subtitle.get("subtitle_body") or "").strip()
            if body:
                return [MusicLyrics(
                    provider=self._source,
                    provider_id=self._optional_text(subtitle.get("subtitle_id")),
                    synced_lyrics=body,
                    language=self._optional_text(subtitle.get("subtitle_language")),
                    match_score=95,
                    provider_priority=30,
                )]
        lyrics = self._response_item(
            self._request("matcher.lyrics.get", {"q_track": title, "q_artist": artist}),
            "lyrics",
        )
        if not lyrics or lyrics.get("restricted"):
            return []
        body = str(lyrics.get("lyrics_body") or "").strip()
        instrumental = bool(lyrics.get("instrumental"))
        if not body and not instrumental:
            return []
        return [MusicLyrics(
            provider=self._source,
            provider_id=self._optional_text(lyrics.get("lyrics_id")),
            instrumental=instrumental,
            plain_lyrics=body or None,
            language=self._optional_text(lyrics.get("lyrics_language")),
            match_score=92,
            provider_priority=30,
        )]

    def _request(self, method: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        """请求官方 API，并对限流或服务过载设置进程内冷却。"""
        api_key = str(settings.MUSIXMATCH_API_KEY or "").strip()
        if not api_key or time.monotonic() < self._cooldown_until:
            return None
        response = RequestUtils(
            ua=settings.USER_AGENT,
            proxies=settings.PROXY,
            timeout=20,
        ).get_res(
            f"{str(settings.MUSIXMATCH_BASE_URL).rstrip('/')}/{method}",
            params={**params, "apikey": api_key},
        )
        if response is None:
            return None
        try:
            if response.status_code in (429, 503):
                retry_after = self._optional_int(response.headers.get("Retry-After")) or 60
                type(self)._cooldown_until = time.monotonic() + retry_after
                logger.warning(f"Musixmatch 进入冷却 {retry_after} 秒")
                return None
            if response.status_code != 200:
                logger.warning(f"Musixmatch 请求失败：HTTP {response.status_code}")
                return None
            payload = response.json()
            status = self._optional_int(
                ((payload.get("message") or {}).get("header") or {}).get("status_code")
            ) if isinstance(payload, dict) else None
            if status != 200:
                if status in (401, 402, 429):
                    logger.warning(f"Musixmatch API 拒绝请求：状态码 {status}")
                return None
            return payload
        except (TypeError, ValueError) as err:
            logger.warning(f"Musixmatch 响应解析失败：{err}")
            return None
        finally:
            response.close()

    @staticmethod
    def _response_item(payload: Any, name: str) -> Optional[dict[str, Any]]:
        """从官方 message/body 包装中提取歌词或字幕对象。"""
        if not isinstance(payload, dict):
            return None
        item = (((payload.get("message") or {}).get("body") or {}).get(name))
        return item if isinstance(item, dict) else None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """安全转换整数响应字段。"""
        try:
            return int(float(value)) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_text(value: Any) -> Optional[str]:
        """安全转换非空文本字段。"""
        text = str(value or "").strip()
        return text or None
