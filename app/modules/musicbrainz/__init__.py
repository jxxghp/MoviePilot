import threading
import time
from datetime import datetime
from typing import Any, Optional, Tuple, Union

from app.core.config import settings
from app.core.music import MusicInfo, MusicMeta
from app.log import logger
from app.modules import _ModuleBase
from app.schemas.types import MediaRecognizeType, ModuleType
from app.utils.http import RequestUtils


class MusicBrainzModule(_ModuleBase):
    """通过 MusicBrainz 提供音乐元数据搜索和详情识别。"""

    _source = "musicbrainz"
    _base_url = "https://musicbrainz.org/ws/2"
    _detail_url = "https://musicbrainz.org/recording"
    _cover_url = "https://coverartarchive.org/release-group"
    _request_interval = 1.0
    _request_lock = threading.Lock()
    _last_request_at = 0.0

    def init_module(self) -> None:
        """初始化无状态的 MusicBrainz 模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """MusicBrainz 无需独立密钥或启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 MusicBrainz 搜索接口连通性。"""
        result = self._request_json(
            "/recording",
            params={"query": "recording:test", "limit": 1, "fmt": "json"},
        )
        return (True, "") if result is not None else (False, "MusicBrainz 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "MusicBrainz"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的媒体识别类型。"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """返回 MusicBrainz 模块子类型。"""
        return MediaRecognizeType.MusicBrainz

    @staticmethod
    def get_priority() -> int:
        """返回音乐元数据模块执行优先级。"""
        return 5

    def search_music(self, meta: MusicMeta, limit: int = 20) -> list[MusicInfo]:
        """根据标准音乐搜索条件返回 MusicBrainz 录音候选。"""
        query = self._build_query(meta)
        if not query:
            return []
        payload = self._request_json(
            "/recording",
            params={"query": query, "limit": max(1, min(limit, 100)), "fmt": "json"},
        )
        return [
            info
            for item in (payload or {}).get("recordings") or []
            if (info := self._recording_to_info(item))
        ]

    def recognize_music(self, source: str, media_id: str) -> Optional[MusicInfo]:
        """按 MusicBrainz Recording ID 获取标准化音乐详情。"""
        if source != self._source or not media_id:
            return None
        payload = self._request_json(
            f"/recording/{media_id}",
            params={
                "inc": "artists+releases+release-groups+isrcs",
                "fmt": "json",
            },
        )
        return self._recording_to_info(payload) if payload else None

    @classmethod
    def _build_query(cls, meta: MusicMeta) -> str:
        """构造 MusicBrainz Recording 搜索表达式。"""
        clauses = []
        if meta.title:
            clauses.append(f'recording:"{cls._escape_query(meta.title)}"')
        if meta.artists:
            clauses.append(f'artist:"{cls._escape_query(meta.artists[0])}"')
        if meta.album:
            clauses.append(f'release:"{cls._escape_query(meta.album)}"')
        if meta.isrc:
            clauses.append(f'isrc:"{cls._escape_query(meta.isrc)}"')
        return " AND ".join(clauses)

    @staticmethod
    def _escape_query(value: str) -> str:
        """转义 MusicBrainz 查询中的引号和反斜线。"""
        return value.replace("\\", "\\\\").replace('"', '\\"').strip()

    @classmethod
    def _recording_to_info(cls, recording: dict[str, Any]) -> Optional[MusicInfo]:
        """将 MusicBrainz Recording 响应转换为标准音乐信息。"""
        media_id = recording.get("id")
        title = recording.get("title")
        if not media_id or not title:
            return None
        releases = recording.get("releases") or []
        release = cls._select_release(releases)
        release_group = (release or {}).get("release-group") or {}
        release_date = cls._release_date(recording, release)
        album = (release or {}).get("title")
        artists = cls._artist_names(recording.get("artist-credit"))
        album_artists = cls._artist_names((release or {}).get("artist-credit"))
        category_parts = [release_group.get("primary-type")]
        category_parts.extend(release_group.get("secondary-types") or [])
        return MusicInfo(
            source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists,
            album=album,
            album_artist=" / ".join(album_artists) if album_artists else None,
            year=cls._year(release_date),
            release_date=release_date,
            duration=cls._duration_seconds(recording.get("length")),
            isrc=next(iter(recording.get("isrcs") or []), None),
            cover_url=cls._build_cover_url(release_group.get("id")),
            version=recording.get("disambiguation") or None,
            category=" / ".join(str(part) for part in category_parts if part),
            names=[name for name in (title, album) if name],
            detail_link=f"{cls._detail_url}/{media_id}",
            raw_data=recording,
        )

    @staticmethod
    def _artist_names(artist_credit: Optional[list[dict[str, Any]]]) -> list[str]:
        """从 MusicBrainz artist-credit 提取有序艺术家名称。"""
        results = []
        for credit in artist_credit or []:
            artist = credit.get("artist") or {}
            name = artist.get("name") or credit.get("name")
            if name and name not in results:
                results.append(str(name))
        return results

    @classmethod
    def _select_release(cls, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """优先选择正式且日期最早的发行记录。"""
        if not releases:
            return {}
        official = [release for release in releases if release.get("status") == "Official"]
        candidates = official or releases
        return min(
            candidates,
            key=lambda release: cls._date_sort_key(release.get("date")),
        )

    @staticmethod
    def _date_sort_key(value: Optional[str]) -> tuple[int, str]:
        """将完整或不完整发行日期转换为稳定排序键。"""
        return (0, value) if value else (1, "")

    @staticmethod
    def _release_date(recording: dict[str, Any], release: dict[str, Any]) -> Optional[str]:
        """从录音和发行信息中选择最可靠的发行日期。"""
        return recording.get("first-release-date") or release.get("date")

    @staticmethod
    def _year(release_date: Optional[str]) -> Optional[int]:
        """从 MusicBrainz 的可变精度日期提取年份。"""
        if not release_date:
            return None
        try:
            return int(release_date[:4])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _duration_seconds(value: Any) -> Optional[int]:
        """将 MusicBrainz 毫秒时长转换为整数秒。"""
        try:
            return round(int(value) / 1000) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _build_cover_url(cls, release_group_id: Optional[str]) -> Optional[str]:
        """根据 Release Group ID 构造 Cover Art Archive 封面地址。"""
        if not release_group_id:
            return None
        return f"{cls._cover_url}/{release_group_id}/front-500"

    @classmethod
    def _wait_for_rate_limit(cls) -> None:
        """串行控制 MusicBrainz 公共接口的最小请求间隔。"""
        with cls._request_lock:
            now = time.monotonic()
            remaining = cls._request_interval - (now - cls._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            cls._last_request_at = time.monotonic()

    @classmethod
    def _request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 MusicBrainz JSON 接口并统一处理网络和响应错误。"""
        cls._wait_for_rate_limit()
        response = RequestUtils(
            headers={
                "User-Agent": f"{settings.USER_AGENT} (https://github.com/jxxghp/MoviePilot)",
                "Accept": "application/json",
            },
            proxies=settings.PROXY,
            timeout=20,
        ).get_res(f"{cls._base_url}{path}", params=params)
        if not response:
            return None
        try:
            if response.status_code != 200:
                logger.warning(
                    f"MusicBrainz 请求失败：{response.status_code} {response.text[:200]}"
                )
                return None
            return response.json()
        except (TypeError, ValueError) as err:
            logger.warning(f"MusicBrainz 响应解析失败：{err}")
            return None
        finally:
            response.close()
