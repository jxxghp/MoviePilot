"""整理任务领域对象的共享类型收窄与字典投影。"""

from typing import Any, Optional, Protocol, cast

from app.domain.context import MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic

_MUSIC_FIELDS = ("title", "album", "artists", "album_artist", "disc_number", "track_number", "total_discs", "total_tracks",
                 "year", "musicbrainz_release_id", "musicbrainz_release_group_id")
_MUSIC_PENDING_STATES = {"not_found", "ambiguous", "conflict", "service_error", "budget_exhausted"}
_MUSIC_FIELD_SOURCES = {"tag", "album_tags", "stream", "filename", "directory", "torrent", "remote", "manual", "cue"}


class _DictionarySerializable(Protocol):
    """描述领域对象沿用的字典投影能力。"""

    def to_dict(self) -> dict[str, Any]:
        """返回领域对象的字典投影。"""


class _TransferTaskMetaSource(Protocol):
    """描述整理任务提供已解析领域元数据的最小形状。"""

    meta: MetaBase | None


def domain_to_dict(value: object) -> dict[str, Any]:
    """按领域对象既有 ``to_dict`` 合同生成字典投影。"""
    return cast(_DictionarySerializable, value).to_dict()


def transfer_task_meta(task: _TransferTaskMetaSource) -> MetaBase:
    """声明进入作业与规划边界的整理任务已完成元数据解析。"""
    return cast(MetaBase, task.meta)


def _music_preview_status(meta: MetaMusic, info: Optional[MusicInfo], diagnostic: dict[str, Any], selected: bool) -> str:
    """只把实际来源核验称为在线确认；标签中的ID和历史未知来源不能据此升级状态。"""
    if meta.organization_error:
        return "conflict"
    status = str(diagnostic.get("status") or "")
    if status in _MUSIC_PENDING_STATES:
        return status
    identity_source = info.field_sources.get("media_id") if info else None
    if info and info.media_id and (selected or identity_source == "manual"):
        return "manual"
    if info and info.media_id and (status == "matched" or identity_source == "remote"):
        return "matched"
    if status in {"local_tags", "local_cue"}:
        return status
    return "metadata" if info and info.title else "not_found"


def _music_preview_candidates(diagnostic: dict[str, Any]) -> list[dict[str, str]]:
    """裁剪来源诊断，保留有界候选身份与标题，不透传原始响应或把评分当置信概率。"""
    candidates = diagnostic.get("candidates")
    if not isinstance(candidates, list):
        return []
    keys = ("media_source", "media_id", "music_type", "release_id", "album_id", "title", "artist", "year")
    return [{key: str(item[key]) for key in keys if isinstance(item.get(key), (str, int))}
            for item in candidates[:5] if isinstance(item, dict)]


def music_transfer_preview(meta: Optional[MetaBase], media: object, context: dict[str, Any], *, selected: bool) -> Optional[dict[str, Any]]:
    """投影音乐识别与扫描证据，不读取文件、不请求服务，也不改变整理的准入或执行结果。"""
    if not isinstance(meta, MetaMusic):
        return None
    info = media if isinstance(media, MusicInfo) else None
    value = info or meta
    diagnostic = info.raw_data.get("recognition") if info else None
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    status = _music_preview_status(meta, info, diagnostic, selected)
    sources = {**meta.field_sources, **(info.field_sources if info else {})}
    fields = (*_MUSIC_FIELDS, "year", "original_year", "release_year", "media_id", "media_source",
              "musicbrainz_release_id", "audio_format", "audio_lossless", "bit_depth", "sample_rate",
              "composers", "conductors", "orchestras", "performers")
    return {
        **context, **{key: getattr(value, key, None) for key in _MUSIC_FIELDS},
        "status": status, "online_confirmed": status == "matched",
        "music_type": value.music_type, "media_id": value.media_id,
        "media_source": getattr(value.media_source, "value", value.media_source),
        "layout": meta.music_layout,
        "field_sources": {key: source for key, source in sources.items() if key in fields and source in _MUSIC_FIELD_SOURCES},
        "candidates": _music_preview_candidates(diagnostic),
    }
