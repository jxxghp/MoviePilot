import re
import threading
import time
from typing import Any, Optional, Tuple, Union

from app.runtime.cache import cached
from app.runtime.config import settings
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.adapters.network.http import RequestUtils


class LrclibModule(_ModuleBase):
    """通过 LRCLIB 获取与单个音轨匹配的同步歌词或纯文本歌词。"""

    _base_url = "https://lrclib.net"
    _source = "lrclib"
    _request_interval = 0.3
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    _match_pattern = re.compile(r"[^\w]+", flags=re.UNICODE)

    def init_module(self) -> None:
        """初始化无状态的 LRCLIB 歌词模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """LRCLIB 无需密钥，是否请求由音乐歌词刮削策略控制。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 LRCLIB 搜索接口连通性。"""
        result = self._request_json("/api/search", params={"track_name": "test"})
        return (True, "") if result is not None else (False, "LRCLIB 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "LRCLIB"

    @staticmethod
    def get_priority() -> int:
        """返回歌词模块执行优先级。"""
        return 5

    def music_lyrics(self, music: Union[MetaMusic, MusicInfo]) -> Optional[MusicLyrics]:
        """按标题、艺术家、专辑和时长查询单曲歌词，并对搜索回退结果严格匹配。"""
        title = str(getattr(music, "title", None) or "").strip()
        artists = list(getattr(music, "artists", None) or [])
        artist = str((artists[0] if artists else getattr(music, "album_artist", None)) or "").strip()
        album = str(getattr(music, "album", None) or "").strip()
        duration = self._optional_int(getattr(music, "duration", None))
        if not title or not artist:
            return None

        exact_params: dict[str, Any] = {
            "track_name": title,
            "artist_name": artist,
        }
        if album:
            exact_params["album_name"] = album
        if duration:
            exact_params["duration"] = duration
        payload = self._request_json("/api/get", params=exact_params)
        if not payload:
            results = self._request_json(
                "/api/search",
                params={
                    "track_name": title,
                    "artist_name": artist,
                    **({"album_name": album} if album else {}),
                },
            )
            payload = self._select_result(
                results if isinstance(results, list) else [],
                title=title,
                artist=artist,
                album=album,
                duration=duration,
            )
        return self._to_lyrics(payload)

    @classmethod
    def _select_result(
            cls,
            results: list[dict[str, Any]],
            title: str,
            artist: str,
            album: str,
            duration: Optional[int],
    ) -> Optional[dict[str, Any]]:
        """从模糊搜索结果中选择标题和艺术家一致且时长可信的歌词。"""
        expected_title = cls._normalize_text(title)
        expected_artist = cls._normalize_text(artist)
        expected_album = cls._normalize_text(album)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in results:
            if cls._normalize_text(item.get("trackName")) != expected_title:
                continue
            candidate_artist = cls._normalize_text(item.get("artistName"))
            if not cls._compatible_text(expected_artist, candidate_artist):
                continue
            candidate_duration = cls._optional_int(item.get("duration"))
            if duration and candidate_duration and abs(duration - candidate_duration) > 2:
                continue
            score = 4
            if candidate_artist == expected_artist:
                score += 3
            if expected_album and cls._normalize_text(item.get("albumName")) == expected_album:
                score += 2
            if duration and candidate_duration and abs(duration - candidate_duration) <= 2:
                score += 3
            ranked.append((score, item))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1]

    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        """移除大小写、标点和空白差异，生成歌词匹配文本。"""
        return cls._match_pattern.sub("", str(value or "").casefold())

    @staticmethod
    def _compatible_text(expected: str, candidate: str) -> bool:
        """允许合作艺人字符串互相包含，同时拒绝完全无关的艺术家。"""
        return bool(expected and candidate and (expected in candidate or candidate in expected))

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """把歌词源返回的时长安全转换为整数秒。"""
        try:
            return round(float(value)) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _to_lyrics(cls, payload: Any) -> Optional[MusicLyrics]:
        """把 LRCLIB 响应转换为标准歌词对象。"""
        if not isinstance(payload, dict) or payload.get("id") is None:
            return None
        plain_lyrics = str(payload.get("plainLyrics") or "").strip() or None
        synced_lyrics = str(payload.get("syncedLyrics") or "").strip() or None
        instrumental = bool(payload.get("instrumental"))
        if not instrumental and not plain_lyrics and not synced_lyrics:
            return None
        return MusicLyrics(
            provider=cls._source,
            provider_id=str(payload["id"]),
            instrumental=instrumental,
            plain_lyrics=plain_lyrics,
            synced_lyrics=synced_lyrics,
        )

    @classmethod
    def _request_once(
            cls,
            path: str,
            params: Optional[dict[str, Any]],
    ) -> Any:
        """串行执行一次 LRCLIB 请求，确保批量专辑刮削遵守最小请求间隔。"""
        with cls._request_lock:
            delay = cls._request_interval - (time.monotonic() - cls._last_request_at)
            if delay > 0:
                time.sleep(delay)
            response = RequestUtils(
                headers={
                    "User-Agent": f"{settings.USER_AGENT} (https://github.com/jxxghp/MoviePilot)",
                    "Accept": "application/json",
                },
                proxies=settings.PROXY,
                timeout=20,
            ).get_res(f"{cls._base_url}{path}", params=params)
            cls._last_request_at = time.monotonic()
            return response

    @classmethod
    @cached(maxsize=1024, ttl=7 * 24 * 60 * 60, skip_none=True)
    def _request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Any:
        """请求 LRCLIB JSON 接口，缓存命中与未命中结果并按 Retry-After 重试一次。"""
        response = cls._request_once(path, params)
        if response is None:
            return None
        try:
            if response.status_code == 404:
                return {}
            if response.status_code in (429, 503):
                retry_after = cls._retry_after_seconds(response.headers.get("Retry-After"))
                response.close()
                time.sleep(retry_after)
                response = cls._request_once(path, params)
                if response is None:
                    return None
                if response.status_code == 404:
                    return {}
            if response.status_code != 200:
                logger.warning(
                    f"LRCLIB 请求失败：{response.status_code} {response.text[:200]}"
                )
                return None
            return response.json()
        except (TypeError, ValueError) as err:
            logger.warning(f"LRCLIB 响应解析失败：{err}")
            return None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _retry_after_seconds(value: Any) -> float:
        """解析 LRCLIB 限流等待秒数，异常值回退到一秒。"""
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            return 1.0
