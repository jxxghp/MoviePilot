"""Chain durable 事件的事务写端口与可重放 payload 转换。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from app.application.history import TransferHistoryRecord, TransferHistoryWriter
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType


class ChainDurableEventWriter(Protocol):
    """下载与整理 Chain 原子写业务记录和 outbox 的宿主端口。"""

    def download_added(
        self,
        *,
        history_payload: dict[str, Any],
        file_payloads: list[dict[str, Any]],
        event_payload: dict[str, Any],
        after_commit: Callable[[], None],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        """提交下载历史与 DownloadAdded intent，再执行原有提交后编排。"""

    def transfer_result(
        self,
        *,
        topic: str,
        stage_history: Callable[[TransferHistoryWriter], TransferHistoryRecord | None],
        event_payload: dict[str, Any],
        publish: Callable[[dict[str, Any]], None],
    ) -> TransferHistoryRecord | None:
        """提交整理历史与结果 intent，并在提交后广播兼容事件。"""


@dataclass(frozen=True, slots=True)
class TransferHistoryRef:
    """事务关闭后仍可安全读取的最小整理历史投影。"""

    id: int
    status: bool
    src: str | None
    src_storage: str | None
    src_fileitem: dict[str, Any] | None


def download_added_event_key(history_id: int) -> str:
    """由下载历史 ID 构造本次下载事实稳定的 DownloadAdded 幂等键。"""
    return f"download.added:{history_id}:v1"


def transfer_result_event_key(topic: str, history_id: int) -> str:
    """由结果 topic 与整理历史 ID 构造稳定幂等键。"""
    return f"{topic}:{history_id}:v1"


def snapshot_download_added(payload: dict[str, Any]) -> dict[str, Any]:
    """把插件运行时 Context 转为 outbox 可 JSON 序列化的稳定快照。"""
    context = payload.get("context")
    return cast(dict[str, Any], _json_snapshot({
        "hash": payload.get("hash"),
        "context": context.to_dict() if isinstance(context, Context) else context,
        "username": payload.get("username"),
        "downloader": payload.get("downloader"),
        "episodes": list(payload.get("episodes") or []),
        "source": payload.get("source"),
        "idempotency_key": payload.get("idempotency_key"),
    }))


def restore_download_added(payload: dict[str, Any]) -> dict[str, Any]:
    """从 outbox 快照恢复插件既有 DownloadAdded 运行时对象形状。"""
    restored = dict(payload)
    context = payload.get("context")
    if isinstance(context, dict):
        restored["context"] = _restore_context(context)
    return restored


def snapshot_transfer_result(payload: dict[str, Any]) -> dict[str, Any]:
    """把整理事件中的领域对象转换为可恢复 JSON 快照。"""
    return cast(dict[str, Any], _json_snapshot({
        "fileitem": _model_snapshot(payload.get("fileitem")),
        "meta": _object_snapshot(payload.get("meta")),
        "mediainfo": _object_snapshot(payload.get("mediainfo")),
        "transferinfo": _model_snapshot(payload.get("transferinfo")),
        "downloader": payload.get("downloader"),
        "download_hash": payload.get("download_hash"),
        "transfer_history_id": payload.get("transfer_history_id"),
        "idempotency_key": payload.get("idempotency_key"),
    }))


def restore_transfer_result(payload: dict[str, Any]) -> dict[str, Any]:
    """从 outbox 快照恢复 TransferComplete/Failed 的旧对象 payload。"""
    restored = dict(payload)
    fileitem = payload.get("fileitem")
    meta = payload.get("meta")
    mediainfo = payload.get("mediainfo")
    transferinfo = payload.get("transferinfo")
    restored["fileitem"] = (
        FileItem.model_validate(fileitem) if isinstance(fileitem, dict) else fileitem
    )
    restored["meta"] = _restore_meta(meta) if isinstance(meta, dict) else meta
    restored["mediainfo"] = (
        _restore_media(mediainfo) if isinstance(mediainfo, dict) else mediainfo
    )
    restored["transferinfo"] = (
        TransferInfo.model_validate(transferinfo)
        if isinstance(transferinfo, dict)
        else transferinfo
    )
    return restored


def _model_snapshot(value: Any) -> Any:
    """序列化 Pydantic 风格对象，空值和已有 JSON 值原样返回。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _object_snapshot(value: Any) -> Any:
    """序列化领域对象，避免把不可持久化实例写入 JSON 列。"""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return _model_snapshot(value)


def _json_snapshot(value: Any) -> Any:
    """递归归一化快照，确保 SQLAlchemy JSON 不接收运行时专用对象。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_snapshot(value.value)
    if isinstance(value, (Path, date, datetime)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, dict):
        return {
            str(key): _json_snapshot(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_snapshot(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_snapshot(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _json_snapshot(value.to_dict())
    return str(value)


def _restore_context(payload: dict[str, Any]) -> Context:
    """恢复 DownloadAdded 插件依赖的 Context 聚合对象。"""
    meta_payload = payload.get("meta_info")
    media_payload = payload.get("media_info")
    torrent_payload = payload.get("torrent_info")
    allowed_episodes = payload.get("allowed_episodes")
    return Context(
        meta_info=(
            _restore_meta(meta_payload) if isinstance(meta_payload, dict) else None
        ),
        media_info=(
            _restore_media(media_payload) if isinstance(media_payload, dict) else None
        ),
        torrent_info=(
            _restore_torrent(torrent_payload)
            if isinstance(torrent_payload, dict)
            else None
        ),
        media_recognize_fail_count=int(
            payload.get("media_recognize_fail_count") or 0
        ),
        resource_source=str(payload.get("resource_source") or "unknown"),
        match_source=str(payload.get("match_source") or "unknown"),
        candidate_recognized=bool(payload.get("candidate_recognized")),
        media_info_is_target=bool(payload.get("media_info_is_target")),
        allowed_episodes=(
            set(allowed_episodes) if allowed_episodes is not None else None
        ),
        confirmed_full_coverage=bool(payload.get("confirmed_full_coverage")),
    )


def _restore_meta(payload: dict[str, Any]) -> MetaBase:
    """恢复影视或音乐文件名解析对象，并保留快照中的解析字段。"""
    if payload.get("type") in {MediaType.MUSIC, MediaType.MUSIC.value, "music"}:
        return MetaMusic.from_dict(payload)
    title = str(
        payload.get("org_string")
        or payload.get("title")
        or payload.get("name")
        or ""
    )
    meta = MetaInfo(title)
    for key, value in payload.items():
        if key in {"season_episode", "edition", "name", "episode_list"}:
            continue
        if key == "type" and value:
            value = MediaType(value)
        setattr(meta, key, value)
    return meta


def _restore_media(payload: dict[str, Any]) -> MediaInfo | MusicInfo:
    """恢复影视或音乐媒体对象，不触发任何远端识别。"""
    if payload.get("type") in {MediaType.MUSIC, MediaType.MUSIC.value, "music"}:
        return MusicInfo.from_dict(payload)
    media = MediaInfo()
    media.from_dict(payload)
    return media


def _restore_torrent(payload: dict[str, Any]) -> TorrentInfo:
    """按 dataclass 构造字段恢复种子对象，忽略快照中的计算属性。"""
    allowed = {item.name for item in fields(TorrentInfo) if item.init}
    return TorrentInfo(**{key: value for key, value in payload.items() if key in allowed})
