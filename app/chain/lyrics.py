import time
from typing import Any, Optional, Union

from app.chain import ChainBase
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic


class LyricsChain(ChainBase):
    """聚合本地、插件和宿主歌词候选，并按匹配度与内容质量择优。"""

    def get_music_lyrics_candidates(
            self,
            music: Union[MetaMusic, MusicInfo],
            local_candidates: Optional[list[MusicLyrics]] = None,
    ) -> list[MusicLyrics]:
        """获取所有标准化歌词候选，同时兼容旧版单结果插件接口。"""
        candidates = list(local_candidates or [])
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.budget_exceeded = True
            return self._deduplicate(candidates)
        legacy = self.run_module("music_lyrics", music=music)
        candidates.extend(self._normalize_results(legacy))
        results = self.run_module("music_lyrics_candidates", music=music)
        candidates.extend(self._normalize_results(results))

        fallback = str(getattr(music, "lyrics", None) or "").strip()
        if fallback:
            candidates.append(MusicLyrics(
                provider="theaudiodb",
                plain_lyrics=fallback,
                match_score=85,
                provider_priority=10,
            ))
        return self._deduplicate(candidates)

    def get_music_lyrics(
            self,
            music: Union[MetaMusic, MusicInfo],
            local_candidates: Optional[list[MusicLyrics]] = None,
    ) -> Optional[MusicLyrics]:
        """返回匹配可信且质量最高的歌词候选。"""
        candidates = self.get_music_lyrics_candidates(music, local_candidates)
        return self._select_best(candidates)

    async def async_get_music_lyrics_candidates(
            self,
            music: Union[MetaMusic, MusicInfo],
            local_candidates: Optional[list[MusicLyrics]] = None,
    ) -> list[MusicLyrics]:
        """异步聚合新旧模块候选，并复用同步路径的本地兜底规则。"""
        candidates = list(local_candidates or [])
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.budget_exceeded = True
            return self._deduplicate(candidates)
        legacy = await self.async_run_module("music_lyrics", music=music)
        candidates.extend(self._normalize_results(legacy))
        results = await self.async_run_module("music_lyrics_candidates", music=music)
        candidates.extend(self._normalize_results(results))
        fallback = str(getattr(music, "lyrics", None) or "").strip()
        if fallback:
            candidates.append(MusicLyrics(
                provider="theaudiodb",
                plain_lyrics=fallback,
                match_score=85,
                provider_priority=10,
            ))
        return self._deduplicate(candidates)

    async def async_get_music_lyrics(
            self,
            music: Union[MetaMusic, MusicInfo],
            local_candidates: Optional[list[MusicLyrics]] = None,
    ) -> Optional[MusicLyrics]:
        """异步返回质量最高的标准歌词候选。"""
        candidates = await self.async_get_music_lyrics_candidates(music, local_candidates)
        return self._select_best(candidates)

    @staticmethod
    def _select_best(candidates: list[MusicLyrics]) -> Optional[MusicLyrics]:
        """在可信候选中按内容质量、匹配度和来源优先级择优。"""
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.quality_rank,
                item.match_score,
                item.provider_priority,
            ),
        )

    @classmethod
    def _normalize_results(cls, value: Any) -> list[MusicLyrics]:
        """把模块的单结果、列表或字典结果统一转换为候选列表。"""
        values = value if isinstance(value, list) else [value]
        normalized = []
        for item in values:
            if isinstance(item, MusicLyrics):
                normalized.append(item)
            elif isinstance(item, dict):
                normalized.append(MusicLyrics.from_dict(item))
        return normalized

    @staticmethod
    def _deduplicate(candidates: list[MusicLyrics]) -> list[MusicLyrics]:
        """按来源身份和内容去重，重复项保留质量与匹配度更高的一条。"""
        selected: dict[tuple[str, str, str], MusicLyrics] = {}
        for candidate in candidates:
            if not candidate or not (candidate.content or candidate.instrumental):
                continue
            current = selected.get(candidate.identity_key)
            if current is None or (
                    candidate.match_score,
                    candidate.quality_rank,
                    candidate.provider_priority,
            ) > (
                    current.match_score,
                    current.quality_rank,
                    current.provider_priority,
            ):
                selected[candidate.identity_key] = candidate
        return list(selected.values())
    def __init__(self, deadline: Optional[float] = None) -> None:
        """保存批次查询截止时间，防止单张专辑长期占用刮削任务。"""
        super().__init__()
        self.deadline = deadline
        self.budget_exceeded = False
