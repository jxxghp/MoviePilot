"""Chain 持久事件的事务写端口与可重放 payload 转换。"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from app.application.history import (
    DownloadFileWrite,
    DownloadHistoryWrite,
    TransferHistorySnapshot,
    TransferHistoryStagingPort,
)
from app.application.transfer.execution import TransferSettlementResult
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.file import FileItem
from app.schemas.message import Message
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType


class ChainDurableEventWriter(Protocol):
    """下载与整理 Chain 原子写业务记录和 outbox 的宿主端口。"""

    def download_added(
        self,
        *,
        history: DownloadHistoryWrite,
        files: tuple[DownloadFileWrite, ...],
        event_payload: dict[str, Any],
        notification_payload: dict[str, Any] | None,
        processing_payload: dict[str, Any],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        """原子提交下载历史及事件、通知和后处理 intent。"""

    def transfer_result(
        self,
        *,
        topic: str | None,
        stage_history: Callable[
            [TransferHistoryStagingPort],
            TransferHistorySnapshot | None,
        ],
        event_payload: dict[str, Any],
        publish: Callable[[dict[str, Any]], None] | None,
        settlement: "TransferResultSettlement | None" = None,
    ) -> TransferHistorySnapshot | TransferSettlementResult | None:
        """提交历史、可选任务终态和结果 intent，再按 topic 广播事件。"""


@dataclass(frozen=True, slots=True)
class TransferResultSettlement:
    """描述一次受 lease fencing 保护的整理任务终态结算。"""

    task_id: str
    lease_token: str
    execution_fingerprint: str
    outcome: str
    error: str | None = None

    def __post_init__(self) -> None:
        """拒绝缺少稳定身份、非法结果或不可诊断的失败终态。"""
        if not self.task_id or not self.lease_token or not self.execution_fingerprint:
            raise ValueError("整理终态结算缺少任务、租约或执行检查点身份")
        if self.outcome not in {"succeeded", "failed"}:
            raise ValueError(f"不支持的整理终态：{self.outcome}")
        if self.outcome == "failed" and not self.error:
            raise ValueError("整理失败终态必须包含可诊断原因")


def download_added_event_key(history_id: int) -> str:
    """由下载历史 ID 与本次事实标识构造 DownloadAdded 幂等键。"""
    return f"download.added:{history_id}:{uuid4().hex}:v1"


def download_effect_event_key(event_key: str, topic: str) -> str:
    """由同一次下载事实键派生具名效果键，保持跨 topic 关联稳定。"""
    prefix = "download.added:"
    if not event_key.startswith(prefix):
        raise ValueError("下载效果键必须由 DownloadAdded 事实键派生")
    return f"{topic}:{event_key.removeprefix(prefix)}"


@dataclass(frozen=True, slots=True)
class DownloadProcessingSnapshot:
    """从持久快照恢复的单次下载后处理输入。"""

    context: Context
    download_dir: Path
    torrent_content: str | bytes
    download_hash: str | None = None
    downloader: str | None = None


def snapshot_download_notification(message: Message | None) -> dict[str, Any] | None:
    """冻结已渲染下载通知，使恢复过程不依赖易变模板和领域对象。"""
    if message is None:
        return None
    return {"message": message.model_dump(mode="json")}


def snapshot_download_processing(
    *,
    context: Context,
    download_dir: Path,
    torrent_content: str | bytes,
    download_hash: str | None = None,
    downloader: str | None = None,
) -> dict[str, Any]:
    """冻结下载后处理输入，并保留查询下载器实际内容路径所需的身份。"""
    if isinstance(torrent_content, bytes):
        content = {
            "kind": "bytes",
            "value": base64.b64encode(torrent_content).decode("ascii"),
        }
    else:
        content = {"kind": "text", "value": torrent_content}
    return cast(dict[str, Any], _json_snapshot({
        "context": context.to_dict(),
        "download_dir": download_dir.as_posix(),
        "torrent_content": content,
        "download_hash": download_hash,
        "downloader": downloader,
    }))


def restore_download_processing(payload: dict[str, Any]) -> DownloadProcessingSnapshot:
    """恢复下载后处理输入，并拒绝无法安全重放的持久快照。"""
    context = payload.get("context")
    content = payload.get("torrent_content")
    download_dir = payload.get("download_dir")
    download_hash = payload.get("download_hash")
    downloader = payload.get("downloader")
    if not isinstance(context, dict) or not isinstance(content, dict):
        raise ValueError("下载后处理快照缺少 context 或 torrent_content")
    if not isinstance(download_dir, str) or not download_dir:
        raise ValueError("下载后处理快照缺少 download_dir")
    if download_hash is not None and not isinstance(download_hash, str):
        raise ValueError("下载后处理快照的 download_hash 无效")
    if downloader is not None and not isinstance(downloader, str):
        raise ValueError("下载后处理快照的 downloader 无效")
    kind = content.get("kind")
    value = content.get("value")
    if not isinstance(value, str):
        raise ValueError("下载后处理快照的 torrent_content 无效")
    if kind == "bytes":
        try:
            restored_content: str | bytes = base64.b64decode(
                value.encode("ascii"),
                validate=True,
            )
        except (ValueError, UnicodeEncodeError) as error:
            raise ValueError("下载后处理快照的种子字节无效") from error
    elif kind == "text":
        restored_content = value
    else:
        raise ValueError("下载后处理快照的 torrent_content 类型无效")
    return DownloadProcessingSnapshot(
        context=_restore_context(context),
        download_dir=Path(download_dir),
        torrent_content=restored_content,
        download_hash=download_hash,
        downloader=downloader,
    )


def transfer_result_event_key(
    topic: str,
    history_id: int,
    *,
    settlement: TransferResultSettlement | None = None,
    settlement_revision: int | None = None,
) -> str:
    """为旧结果事实生成 occurrence key，为任务结算生成确定性幂等键。"""
    if settlement is not None:
        if settlement_revision is None or settlement_revision <= 0:
            raise ValueError("整理终态事件键缺少有效结算修订号")
        return (
            f"transfer.result:{settlement.task_id}:{settlement_revision}:"
            f"{settlement.outcome}:v1"
        )
    if settlement_revision is not None:
        raise ValueError("旧整理结果事件键不能单独指定结算修订号")
    return f"{topic}:{history_id}:{uuid4().hex}:v1"


def snapshot_download_added(payload: dict[str, Any]) -> dict[str, Any]:
    """把插件运行时 Context 转为 outbox 可 JSON 序列化的稳定快照。"""
    context = payload.get("context")
    return cast(dict[str, Any], _json_snapshot({
        "hash": payload.get("hash"),
        "context": (
            context.to_dict()
            if isinstance(context, Context)
            else context
        ),
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
        torrent_info=cast(TorrentInfo, (
            _restore_torrent(torrent_payload)
            if isinstance(torrent_payload, dict)
            else None
        )),
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
