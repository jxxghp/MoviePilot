"""音乐来源的有序识别回退；来源失败隔离，身份、候选校验由注入的业务入口负责。"""

from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar

from app.application.music.observation import (
    MusicRecognitionObservation,
    capture_music_recognition,
    capture_music_source,
    music_recognition_failed,
    music_recognition_is_blocked,
    report_music_recognition,
)
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.runtime.log import logger
from app.schemas.types import MediaSource

_Result = TypeVar("_Result")
_Candidate = TypeVar("_Candidate", MusicInfo, MusicAlbumInfo)


def unique_music_match(candidates: Sequence[_Candidate]) -> Optional[_Candidate]:
    """仅接受一个不同来源身份的合理候选；重复行不制造歧义，不同ID不能按返回顺序选中。"""
    unique = {(item.media_source, item.media_id or id(item)): item for item in candidates}
    if len(unique) > 1:
        report_music_recognition("ambiguous", "存在多个同样符合证据的音乐候选，请手动选择", [
            {"media_source": getattr(item.media_source, "value", item.media_source), "media_id": item.media_id,
             "title": item.title, "artist": item.artist} for item in list(unique.values())[:5]
        ])
        return None
    return next(iter(unique.values()), None)


def _source_result(result: Any, observation: MusicRecognitionObservation, accepted: bool) -> bool:
    """成功须经过调用方核验；来源已报告的歧义、冲突或未完成查询不能成为成功结果。"""
    if music_recognition_is_blocked(result):
        diagnostic = result.raw_data["recognition"]
        report_music_recognition(diagnostic["status"], diagnostic.get("message", ""), diagnostic.get("candidates"))
    if observation.status in {"ambiguous", "conflict"} or music_recognition_failed():
        return False
    if accepted:
        report_music_recognition("matched", "音乐来源已匹配当前证据")
    return accepted


def _finish_sources(observation: MusicRecognitionObservation, matched: bool) -> dict[str, Any]:
    """汇总所有来源的终态；歧义优先，全部未命中时保留服务故障而非错误缓存为空。"""
    if matched:
        report_music_recognition("matched", "音乐来源已匹配当前证据")
    elif observation.sources:
        reports = observation.sources
        selected = next((row for row in reports if row["status"] in {"ambiguous", "conflict"}), None)
        selected = selected or next((row for row in reports if row["status"] in {"service_error", "budget_exhausted"}), reports[-1])
        report_music_recognition(selected["status"], selected["message"], selected.get("candidates"))
    return observation.to_dict()


def recognize_music_sources(
        sources: Sequence[MediaSource],
        recognize: Callable[[MediaSource], Optional[_Result]],
        accepts: Callable[[Optional[_Result], MediaSource], bool],
) -> tuple[Optional[_Result], dict[str, Any]]:
    """依次核验最多三个内置来源，每来源8次/45秒且服从更小的外层总预算。"""
    selected = tuple(dict.fromkeys(sources))[:3]
    with capture_music_recognition(seconds=45 * len(selected), request_limit=8 * len(selected)) as total:
        for source in selected:
            with capture_music_source(source.value) as attempt:
                try:
                    result = recognize(source)
                except Exception:
                    logger.warning(f"音乐来源 {source} 识别失败")
                    report_music_recognition("service_error", "音乐元数据来源暂时不可用")
                    result = None
                matched = _source_result(result, attempt, accepts(result, source))
            if matched:
                return result, _finish_sources(total, True)
            if attempt.status in {"ambiguous", "conflict"}:
                break
        return None, _finish_sources(total, False)


async def async_recognize_music_sources(
        sources: Sequence[MediaSource],
        recognize: Callable[[MediaSource], Awaitable[Optional[_Result]]],
        accepts: Callable[[Optional[_Result], MediaSource], bool],
) -> tuple[Optional[_Result], dict[str, Any]]:
    """异步回退沿用相同来源次序、独立诊断和共享预算；取消继续向调用方传播。"""
    selected = tuple(dict.fromkeys(sources))[:3]
    with capture_music_recognition(seconds=45 * len(selected), request_limit=8 * len(selected)) as total:
        for source in selected:
            with capture_music_source(source.value) as attempt:
                try:
                    result = await recognize(source)
                except Exception:
                    logger.warning(f"音乐来源 {source} 识别失败")
                    report_music_recognition("service_error", "音乐元数据来源暂时不可用")
                    result = None
                matched = _source_result(result, attempt, accepts(result, source))
            if matched:
                return result, _finish_sources(total, True)
            if attempt.status in {"ambiguous", "conflict"}:
                break
        return None, _finish_sources(total, False)
