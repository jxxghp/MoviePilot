"""已有下载任务的资源目录分类应用服务。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, cast

from app.application.classification.reference import (
    apply_persisted_classification_snapshot,
    persisted_classification_snapshot,
)
from app.application.directory import (
    DirectoryHelper,
    build_media_download_path,
    validate_download_save_path,
)
from app.application.history import DownloadHistorySnapshot
from app.domain.classification.validation import validate_classification_category_path
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType


@dataclass(frozen=True, slots=True)
class DownloadSourceClassificationPlan:
    """下载器资源目录重新分类的无副作用计划。"""

    current_save_path: str
    target_save_path: str
    category: str
    changed: bool


def _history_media(history: DownloadHistorySnapshot) -> MediaInfo | MusicInfo:
    """从下载历史恢复计算目录所需的媒体与冻结分类事实。"""
    try:
        media_type = MediaType(history.type)
    except ValueError as error:
        raise ValueError(f"下载历史媒体类型无效：{history.type}") from error

    if media_type == MediaType.MUSIC:
        note = history.note
        music_note = note.get("music") if isinstance(note, dict) else None
        if isinstance(music_note, dict):
            media_payload = music_note.get("media")
            music_version = music_note.get("version")
        else:
            media_payload = None
            music_version = None
        if isinstance(media_payload, dict) and music_version == 1:
            media: MediaInfo | MusicInfo = MusicInfo.from_dict(media_payload)
        else:
            try:
                year = int(history.year) if history.year else None
            except (TypeError, ValueError):
                year = None
            media = MusicInfo(
                media_source=history.media_source,
                media_id=history.media_id,
                music_type=history.music_type or "recording",
                title=history.title,
                year=year,
            )
    else:
        media = MediaInfo(
            type=media_type,
            title=history.title or "",
            year=history.year or "",
        )
        if history.media_source and history.media_id:
            media.media_source = history.media_source
            media.media_id = history.media_id

    snapshot = persisted_classification_snapshot(
        category_id=history.media_category_id,
        category_path=history.media_category,
        rule_id=history.classification_rule_id,
        policy_revision=history.classification_policy_revision,
        source=history.classification_source,
    )
    return cast(
        MediaInfo | MusicInfo,
        apply_persisted_classification_snapshot(media, snapshot) or media,
    )


def resolve_download_source_classification(
    torrent: DownloaderTorrent,
    history: DownloadHistorySnapshot,
    media_category: Optional[str] = None,
    directory_helper: Optional[DirectoryHelper] = None,
) -> DownloadSourceClassificationPlan:
    """按已配置下载根目录与历史分类快照计算任务目标位置。"""
    current_save_path = str(torrent.save_path or "").strip()
    if not current_save_path:
        raise ValueError("下载器未返回任务保存目录")

    helper = directory_helper or DirectoryHelper()
    media = _history_media(history)
    if media_category:
        manual_path = validate_classification_category_path(
            tuple(segment.strip() for segment in media_category.split("/") if segment.strip())
        )
        if manual_path not in helper.classification_category_paths(media.type):
            raise ValueError("手动指定的媒体分类不存在、已停用或与媒体类型不匹配")
        media = cast(
            MediaInfo | MusicInfo,
            apply_persisted_classification_snapshot(
                media,
                persisted_classification_snapshot(
                    category_path=manual_path,
                    source="manual",
                ),
            ) or media,
        )
    directory = helper.get_download_dir_by_task_path(media, current_save_path)
    if not directory or not directory.download_path:
        raise ValueError("当前保存目录不在已配置的资源目录中")
    if not helper.has_fixed_category(directory) and not directory.download_category_folder:
        raise ValueError("当前资源目录未开启按类别分类")

    category = (
        helper.resolve_directory_category(directory, media)
        if helper.has_fixed_category(directory)
        else helper.resolve_media_category(media)
    )
    if not category.usable or not category.path:
        raise ValueError("下载历史没有可用的媒体分类")

    target_path = build_media_download_path(
        Path(directory.download_path),
        directory,
        media,
        directory_helper=helper,
    ).as_posix()
    target_save_path = validate_download_save_path(target_path)
    return DownloadSourceClassificationPlan(
        current_save_path=current_save_path,
        target_save_path=target_save_path,
        category="/".join(category.path),
        changed=current_save_path.rstrip("/") != target_save_path.rstrip("/"),
    )


class DownloadSourceClassificationService:
    """预览并通过下载器安全应用资源目录分类。"""

    def __init__(
        self,
        *,
        list_torrents: Callable[..., list[DownloaderTorrent]],
        get_history_by_hash: Callable[[str], Optional[DownloadHistorySnapshot]],
        update_torrent: Callable[..., dict[str, bool]],
        resolve_plan: Callable[
            [DownloaderTorrent, DownloadHistorySnapshot, Optional[str]],
            DownloadSourceClassificationPlan,
        ] = resolve_download_source_classification,
    ) -> None:
        """注入下载器、历史与路径规划端口。"""
        self._list_torrents = list_torrents
        self._get_history_by_hash = get_history_by_hash
        self._update_torrent = update_torrent
        self._resolve_plan = resolve_plan

    @staticmethod
    def _validate_hash(hash_value: str) -> None:
        """校验 BitTorrent v1 Hash，拒绝模糊任务定位。"""
        if len(hash_value) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in hash_value
        ):
            raise ValueError("hash 格式无效")

    def plan(
        self,
        *,
        hash_value: str,
        downloader: Optional[str] = None,
        execute: bool = False,
        media_category: Optional[str] = None,
    ) -> dict[str, Any]:
        """生成分类计划，只有 execute 为真时才请求下载器移动。"""
        self._validate_hash(hash_value)
        torrents = self._list_torrents(
            hashs=[hash_value],
            downloader=downloader,
            include_all_tags=True,
        ) or []
        torrent = next(
            (
                item
                for item in torrents
                if str(item.hash or "").lower() == hash_value.lower()
            ),
            None,
        )
        if torrent is None:
            raise ValueError("未在下载器中找到该任务")
        resolved_downloader = downloader or torrent.downloader
        if not resolved_downloader:
            raise ValueError("下载任务未标记所属下载器")

        history = self._get_history_by_hash(hash_value)
        if history is None:
            raise ValueError("未找到该任务的下载历史")
        plan = self._resolve_plan(torrent, history, media_category)
        executed = False
        if execute and plan.changed:
            result = self._update_torrent(
                hash_string=hash_value,
                downloader=resolved_downloader,
                save_path=plan.target_save_path,
            ) or {}
            if not result.get("save_path"):
                raise ValueError("下载器移动资源目录失败或不支持修改保存位置")
            executed = True

        return {
            "hash": hash_value,
            "downloader": resolved_downloader,
            "current_save_path": plan.current_save_path,
            "target_save_path": plan.target_save_path,
            "category": plan.category,
            "changed": plan.changed,
            "executed": executed,
        }
