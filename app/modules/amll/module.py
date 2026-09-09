"""AMLL TTML 歌词搜索、录音匹配和下载模块。"""

import json
import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional, cast

from app.adapters.network.http import RequestUtils
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.modules import _ModuleBase
from app.modules.amll.lyrics import parse_lyrics
from app.modules.amll.matching import (
    lyric_id,
    match_score,
    normalize_isrc,
    search_identity,
    text_values,
)
from app.runtime.cache import Cache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import ModuleType, OtherModulesType


class AmllModule(_ModuleBase):
    """聚合 AMLL 原生 API 的可信 TTML 候选，由宿主刮削链统一保存。"""

    _page_size = 20
    _download_limit = 3
    _maximum_cooldown = 300.0
    _cache_region = "app.modules.amll"

    def __init__(self) -> None:
        """建立实例级请求互斥和冷却状态，不在模块构造时请求网络。"""
        super().__init__()
        self._request_lock = threading.Lock()
        self._cooldown_until = 0.0

    def init_module(self) -> None:
        """来源配置变化时清理缓存与冷却，后续查询使用新的服务地址。"""
        with self._request_lock:
            Cache(maxsize=1024, ttl=3600).clear(region=self._cache_region)
            self._cooldown_until = 0.0

    def init_setting(self) -> None:
        """公共歌词服务无需密钥，请求由音乐歌词刮削策略控制。"""

    def stop(self) -> None:
        """当前模块不持有后台任务或持久网络客户端。"""

    def test(self) -> tuple[bool, str]:
        """用有界搜索请求检查原生歌词接口连通性。"""
        response = self._search(self._base_url(), "test", "", "")
        return (True, "") if response is not None else (False, "AMLL TTML API 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回歌词来源展示名称。"""
        return "AMLL TTML"

    @staticmethod
    def get_type() -> ModuleType:
        """返回歌词模块所属的杂项能力分类。"""
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """返回 AMLL 歌词来源的稳定模块标识。"""
        return OtherModulesType.Amll

    @staticmethod
    def get_priority() -> int:
        """在候选聚合阶段按常规歌词来源顺序执行。"""
        return 6

    @staticmethod
    def _base_url() -> str:
        """解析当前配置地址，空地址表示本次不调用该来源。"""
        return str(get_runtime_setting("AMLL_BASE_URL") or "").strip().rstrip("/")

    def music_lyrics_candidates(self, music: MetaMusic | MusicInfo) -> list[MusicLyrics]:
        """优先以 ISRC 获取歌词，否则严格筛选搜索结果后下载至多三个候选。"""
        base_url = self._base_url()
        if not base_url:
            return []
        isrc = normalize_isrc(music.isrc)
        if isrc:
            payload = self._lookup_isrc(base_url, isrc)
            if isinstance(payload, dict) and isrc in {
                normalize_isrc(value) for value in text_values(payload, "isrcs")
            }:
                lyrics = self._to_lyrics(music, payload, score=100)
                if lyrics:
                    return [lyrics]
        return self._search_candidates(music, base_url)

    def _search_candidates(self, music: MetaMusic | MusicInfo, base_url: str) -> list[MusicLyrics]:
        """搜索仅提供候选身份，详情下载后再次核对身份与请求 ID。"""
        title, artists, album = search_identity(music)
        if not title or not artists:
            return []
        response = self._search(base_url, title, artists[0], album)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            return []
        candidates: list[MusicLyrics] = []
        requested: set[str] = set()
        for item in items[:self._page_size]:
            if not isinstance(item, dict) or not match_score(music, item):
                continue
            identifier = lyric_id(item)
            if not identifier or identifier in requested:
                continue
            requested.add(identifier)
            payload = self._get(base_url, identifier)
            if isinstance(payload, dict) and lyric_id(payload) == identifier:
                score = match_score(music, payload)
                lyrics = self._to_lyrics(music, payload, score=score) if score else None
                if lyrics:
                    candidates.append(lyrics)
            if len(requested) >= self._download_limit:
                break
        return candidates

    @staticmethod
    def _to_lyrics(
        music: MetaMusic | MusicInfo, payload: dict[str, Any], score: int,
    ) -> Optional[MusicLyrics]:
        """将确认身份后的 TTML 正文交给来源解析器，保留逐词时间轴。"""
        content = payload.get("lyrics")
        identifier = lyric_id(payload)
        if payload.get("format") != "ttml" or not identifier or not isinstance(content, str):
            return None
        title, artists, _album = search_identity(music)
        titles = text_values(payload, "musicNames")
        source_artists = text_values(payload, "artistNames")
        return parse_lyrics(
            content,
            title=title or next(iter(titles), ""),
            artist=" / ".join(artists or source_artists),
            provider_id=identifier,
            match_score=score,
        )

    def _search(
        self, base_url: str, music_name: str, artist_name: str, album_name: str,
    ) -> Optional[dict[str, Any]]:
        """短期缓存动态搜索，控制单页候选数量并避免宽泛正文查询。"""
        params = {"musicName": music_name, "page": 1, "pageSize": self._page_size}
        if artist_name:
            params["artistName"] = artist_name
        if album_name:
            params["albumName"] = album_name
        return self._cached_json("/v1/lyrics/search", params, base_url, ttl=3600)

    def _get(self, base_url: str, identifier: str) -> Optional[dict[str, Any]]:
        """特定歌词 ID 内容固定，成功下载后使用独立的较长缓存。"""
        return self._cached_json("/v1/lyrics/get", {"id": identifier}, base_url, ttl=7 * 24 * 3600)

    def _lookup_isrc(self, base_url: str, isrc: str) -> Optional[dict[str, Any]]:
        """ISRC 对应的最新版歌词会变化，因此使用短期缓存。"""
        return self._cached_json("/v1/lyrics/get", {"isrc": isrc}, base_url, ttl=3600)

    def _cached_json(
        self, path: str, params: dict[str, Any], base_url: str, ttl: int,
    ) -> Optional[dict[str, Any]]:
        """按来源地址隔离缓存，未命中短期保存，网络失败保持可立即重试。"""
        cache = Cache(maxsize=1024, ttl=3600)
        key = json.dumps([base_url, path, params], ensure_ascii=False, sort_keys=True)
        cached_value = cache.get(key, region=self._cache_region)
        if isinstance(cached_value, dict):
            return cast(dict[str, Any], cached_value)
        result = self._request_json(path, params, base_url)
        if result is not None:
            has_result = bool(result.get("items")) if path.endswith("/search") else bool(result)
            cache.set(key, result, ttl=ttl if has_result else 300, region=self._cache_region)
        return result

    def _request_json(
        self, path: str, params: dict[str, Any], base_url: str,
    ) -> Optional[dict[str, Any]]:
        """串行发起有限超时请求，限流期间跳过该来源并确保释放响应。"""
        with self._request_lock:
            if time.monotonic() < self._cooldown_until:
                return None
            try:
                response = RequestUtils(
                    headers={"Accept": "application/json", "User-Agent": get_runtime_setting("USER_AGENT")},
                    proxies=get_runtime_setting("PROXY"),
                    timeout=10,
                ).get_res(f"{base_url}{path}", params=params)
            except (OSError, ValueError) as error:
                logger.warning(f"AMLL TTML API 网络请求失败：{error}")
                return None
            if response is None:
                return None
            try:
                if response.status_code == 404:
                    return {}
                if response.status_code in (429, 503):
                    delay = self._retry_after_seconds(response.headers.get("Retry-After"))
                    self._cooldown_until = time.monotonic() + delay
                    logger.warning(f"AMLL TTML API 进入冷却 {delay:g} 秒")
                    return None
                if response.status_code != 200:
                    logger.warning(f"AMLL TTML API 请求失败：HTTP {response.status_code}")
                    return None
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") != 200:
                    return None
                data = payload.get("data")
                return cast(dict[str, Any], data) if isinstance(data, dict) else None
            except (TypeError, ValueError) as error:
                logger.warning(f"AMLL TTML API 响应解析失败：{error}")
                return None
            finally:
                response.close()

    @classmethod
    def _retry_after_seconds(cls, value: str | None) -> float:
        """解析秒数或 HTTP 日期，限制异常远期值并避免无限等待。"""
        try:
            delay = float(value or "1")
        except ValueError:
            try:
                target = parsedate_to_datetime(value or "")
                delay = (target - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 1.0
        if not math.isfinite(delay):
            return 1.0
        return min(max(delay, 1.0), cls._maximum_cooldown)
