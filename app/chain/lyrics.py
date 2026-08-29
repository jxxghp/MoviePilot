"""歌词候选聚合与择优处理链。"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generator, Optional, Union, cast

from app.chain.base import ChainBase
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic


class _LyricsAction(Enum):
    """歌词状态机允许调用的模块接口。"""

    LEGACY = "music_lyrics"
    CANDIDATES = "music_lyrics_candidates"


@dataclass(frozen=True, slots=True)
class _LyricsRequest:
    """保存歌词聚合的初始候选、截止状态和本地兜底。"""

    candidates: tuple[MusicLyrics, ...]
    fallback: Optional[MusicLyrics]
    budget_exceeded: bool


@dataclass(frozen=True, slots=True)
class _LyricsOutcome:
    """保存聚合后的稳定候选顺序及预算状态。"""

    candidates: tuple[MusicLyrics, ...]
    budget_exceeded: bool = False


def _normalize_lyrics_results(value: Any) -> list[MusicLyrics]:
    """把模块的单结果、列表或字典结果统一转换为候选列表。"""
    values = value if isinstance(value, list) else [value]
    normalized = []
    for item in values:
        if isinstance(item, MusicLyrics):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(MusicLyrics.from_dict(item))
    return normalized


def _deduplicate_lyrics(candidates: list[MusicLyrics]) -> list[MusicLyrics]:
    """按来源身份和内容去重，并保留首次出现位置。"""
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


def _build_lyrics_request(
    music: Union[MetaMusic, MusicInfo],
    local_candidates: Optional[list[MusicLyrics]],
    deadline: Optional[float],
) -> _LyricsRequest:
    """构造一次歌词聚合请求，截止时间只在进入模块调用前判定。"""
    fallback_text = str(getattr(music, "lyrics", None) or "").strip()
    fallback = (
        MusicLyrics(
            provider="theaudiodb",
            plain_lyrics=fallback_text,
            match_score=85,
            provider_priority=10,
        )
        if fallback_text
        else None
    )
    return _LyricsRequest(
        candidates=tuple(local_candidates or ()),
        fallback=fallback,
        budget_exceeded=deadline is not None and time.monotonic() >= deadline,
    )


def _lyrics_plan(
    request: _LyricsRequest,
) -> Generator[_LyricsAction, Any, _LyricsOutcome]:
    """按旧接口、新接口和本地兜底的唯一顺序聚合歌词。"""
    candidates = list(request.candidates)
    if request.budget_exceeded:
        return _LyricsOutcome(
            candidates=tuple(_deduplicate_lyrics(candidates)),
            budget_exceeded=True,
        )
    legacy = yield _LyricsAction.LEGACY
    candidates.extend(_normalize_lyrics_results(legacy))
    results = yield _LyricsAction.CANDIDATES
    candidates.extend(_normalize_lyrics_results(results))
    if request.fallback:
        candidates.append(request.fallback)
    return _LyricsOutcome(candidates=tuple(_deduplicate_lyrics(candidates)))


class LyricsChain(ChainBase):
    """聚合本地、插件和宿主歌词候选，并按匹配度与内容质量择优。"""

    def get_music_lyrics_candidates(
        self,
        music: Union[MetaMusic, MusicInfo],
        local_candidates: Optional[list[MusicLyrics]] = None,
    ) -> list[MusicLyrics]:
        """获取所有标准化歌词候选，同时兼容旧版单结果插件接口。"""
        request = _build_lyrics_request(music, local_candidates, self.deadline)
        plan = _lyrics_plan(request)
        outcome = _LyricsOutcome(candidates=())
        try:
            action = next(plan)
            while True:
                value = (
                    self.run_module("music_lyrics", music=music)
                    if action is _LyricsAction.LEGACY
                    else self.run_module("music_lyrics_candidates", music=music)
                )
                action = plan.send(value)
        except StopIteration as completed:
            outcome = cast(_LyricsOutcome, completed.value)
        if outcome.budget_exceeded:
            self.budget_exceeded = True
        return list(outcome.candidates)

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
        request = _build_lyrics_request(music, local_candidates, self.deadline)
        plan = _lyrics_plan(request)
        outcome = _LyricsOutcome(candidates=())
        try:
            action = next(plan)
            while True:
                value = (
                    await self.async_run_module("music_lyrics", music=music)
                    if action is _LyricsAction.LEGACY
                    else await self.async_run_module("music_lyrics_candidates", music=music)
                )
                action = plan.send(value)
        except StopIteration as completed:
            outcome = cast(_LyricsOutcome, completed.value)
        if outcome.budget_exceeded:
            self.budget_exceeded = True
        return list(outcome.candidates)

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

    def __init__(self, deadline: Optional[float] = None) -> None:
        """保存批次查询截止时间，防止单张专辑长期占用刮削任务。"""
        super().__init__()
        self.deadline = deadline
        self.budget_exceeded = False
