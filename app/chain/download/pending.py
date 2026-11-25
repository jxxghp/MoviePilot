"""
待确认下载提交的持久化记录、按哈希复核、转正结算与目标释放 owner。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Optional, Set, Union, cast

from app.application.download.pending import (
    DOWNLOAD_PENDING_CONFIRM_TTL_SECONDS,
    ConfirmedPendingDownload,
    DownloadPendingRepository,
    DownloadPendingSnapshot,
    DownloadPendingWrite,
    PendingReconciliationResult,
)
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import torrent as torrent_rules
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.runtime.locks import downloader_task_locks
from app.runtime.log import logger
from app.schemas.download import (
    DownloadTaskAction,
    PendingDownloadCheckResult,
    PendingDownloadCheckStatus,
)
from app.schemas.media import build_media_key, normalize_media_source, resolve_media_identity
from app.schemas.message import Message
from app.schemas.types import (
    MediaType,
    MessageType,
    NotificationChannel,
)

# 数据库瞬时不可用（如 SQLite 锁）时的进程内兜底存储，确保同进程后续轮次仍能拦住替代版本。
_FALLBACK_PENDING_LOCK = threading.RLock()
_FALLBACK_PENDING_RECORDS: dict[str, DownloadPendingSnapshot] = {}


def _clear_fallback_pending_records() -> None:
    """
    清空进程内待确认降级缓存，仅用于测试环境隔离。
    """
    with _FALLBACK_PENDING_LOCK:
        _FALLBACK_PENDING_RECORDS.clear()


def _parse_subscription_id(source: Optional[str]) -> Optional[int]:
    """
    从订阅来源字符串解析整型订阅主键；非订阅或格式异常返回 None。
    """
    if not source or not str(source).startswith("Subscribe|"):
        return None
    try:
        raw_id = json.loads(str(source).split("|", 1)[1]).get("id")
        return int(raw_id) if raw_id is not None and not isinstance(raw_id, bool) else None
    except (ValueError, TypeError, AttributeError):
        return None


def _build_pending_target_key(media: Optional[MediaInfo | MusicInfo]) -> str:
    """
    构造媒体级目标键，用于按电影、音乐或电视剧作品拦截替代版本。
    """
    if media is None:
        return ""
    media_type = getattr(getattr(media, "type", None), "value", getattr(media, "type", None))
    if not media_type:
        return ""
    media_source, media_id = resolve_media_identity(media=media)
    media_key = build_media_key(media_source, media_id)
    if media_key:
        return f"{media_type}:{media_key}"
    title = str(getattr(media, "title", "") or "").strip()
    if not title:
        return ""
    year = str(getattr(media, "year", "") or "").strip()
    return f"{media_type}:{title}:{year}"


def _build_pending_key(
    *,
    target_key: str,
    season: Optional[int],
    episodes: Optional[Set[int]],
    hashes: frozenset[str],
    resource_key: str,
) -> str:
    """
    生成单条待确认提交的稳定唯一键，避免同一目标不同集或不同哈希相互覆盖。
    """
    payload = {
        "target_key": target_key,
        "season": season,
        "episodes": sorted(episodes) if episodes else None,
        "hashes": sorted(hashes),
        "resource": resource_key,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _serialize_episode_selection(
    selection: Optional[torrent_rules.EpisodeSelection],
) -> Optional[dict[str, Any]]:
    """
    将冻结的季集对应关系序列化为 JSON 字典。
    """
    if selection is None:
        return None
    return {
        "season": selection.season,
        "source_season": selection.source_season,
        "episode_map": [list(pair) for pair in selection.episode_map],
    }


def _deserialize_episode_selection(
    raw: Any,
) -> Optional[torrent_rules.EpisodeSelection]:
    """
    从脱敏快照还原季集对应关系；格式不完整时返回 None。
    """
    if not isinstance(raw, Mapping):
        return None
    season = raw.get("season")
    if not isinstance(season, int) or isinstance(season, bool):
        return None
    source_season = raw.get("source_season")
    if source_season is not None and (not isinstance(source_season, int) or isinstance(source_season, bool)):
        source_season = None
    mapping: list[tuple[int, int]] = []
    raw_map = raw.get("episode_map")
    if isinstance(raw_map, (list, tuple)):
        for pair in raw_map:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and all(isinstance(item, int) and not isinstance(item, bool) for item in pair)
            ):
                mapping.append((pair[0], pair[1]))
    return torrent_rules.EpisodeSelection(
        season=season,
        source_season=source_season,
        episode_map=tuple(mapping),
    )


def _mapping_field(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """
    安全取出载荷中的子映射，缺失或类型不符时返回空字典。
    """
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def _restore_media_from_record(record: DownloadPendingSnapshot, payload: Mapping[str, Any]) -> MediaInfo | MusicInfo:
    """
    从待确认记录与脱敏快照重建最小媒体对象，供下载历史和订阅结算使用。
    """
    raw_media = _mapping_field(payload, "media")
    raw_type = raw_media.get("type") or record.media_type
    media_type: Optional[MediaType] = None
    if raw_type:
        try:
            media_type = MediaType(str(raw_type))
        except ValueError:
            media_type = None
    media_source = normalize_media_source(raw_media.get("media_source")) or record.media_source
    media_id = str(raw_media.get("media_id") or record.media_id or "").strip() or None
    year = raw_media.get("year") if raw_media.get("year") is not None else record.year
    title = str(raw_media.get("title") or record.title or "")
    category = str(raw_media.get("category") or "")
    if media_type == MediaType.MUSIC:
        music_year: Optional[int] = None
        if year is not None and str(year).strip().isdigit():
            music_year = int(str(year).strip())
        music_type = str(raw_media.get("music_type") or "track")
        cover_url = str(raw_media["poster_path"]) if raw_media.get("poster_path") else None
        return MusicInfo(
            title=title,
            year=music_year,
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
            category=category,
            cover_url=cover_url,
        )
    media = MediaInfo(
        title=title,
        year=str(year) if year is not None else None,
        type=media_type or MediaType.MOVIE,
        media_source=media_source,
        media_id=media_id,
        category=category,
        episode_group=raw_media.get("episode_group"),
    )
    if raw_media.get("poster_path"):
        media.poster_path = str(raw_media["poster_path"])
    if raw_media.get("backdrop_path"):
        media.backdrop_path = str(raw_media["backdrop_path"])
    return media


def _restore_meta_from_record(
    record: DownloadPendingSnapshot,
    payload: Mapping[str, Any],
    media: MediaInfo | MusicInfo,
) -> MetaBase:
    """
    从待确认快照重建标题元数据，保留原季集范围供历史与订阅完成判断消费。
    """
    raw_meta = _mapping_field(payload, "meta")
    org_string = str(raw_meta.get("org_string") or record.torrent_name or record.title or "")
    meta = MetaInfo(org_string)
    if media.type is not None:
        meta.type = media.type
    raw_seasons = raw_meta.get("season_list")
    if isinstance(raw_seasons, (list, tuple)):
        seasons = [int(item) for item in raw_seasons if isinstance(item, int) and not isinstance(item, bool)]
        if seasons:
            meta.begin_season = seasons[0]
            meta.end_season = seasons[-1] if len(seasons) > 1 else None
    elif record.season is not None and meta.begin_season is None:
        meta.begin_season = record.season
    raw_episodes = raw_meta.get("episode_list")
    if isinstance(raw_episodes, (list, tuple)):
        episodes = [int(item) for item in raw_episodes if isinstance(item, int) and not isinstance(item, bool)]
        if episodes:
            meta.set_episode_list(episodes)
    elif record.episodes:
        meta.set_episode_list(list(record.episodes))
    return meta


def _restore_torrent_from_record(record: DownloadPendingSnapshot, payload: Mapping[str, Any]) -> TorrentInfo:
    """
    从脱敏快照重建非敏感种子信息（不含凭据和下载链接）。
    """
    raw_torrent = _mapping_field(payload, "torrent")
    return TorrentInfo(
        site=raw_torrent.get("site"),
        site_name=raw_torrent.get("site_name"),
        title=str(raw_torrent.get("title") or record.torrent_name or ""),
        description=raw_torrent.get("description"),
        size=raw_torrent.get("size") or 0,
        pri_order=raw_torrent.get("pri_order") or 0,
    )


def _restore_channel(raw_channel: Any) -> Optional[NotificationChannel]:
    """
    还原通知渠道枚举；无效或缺失时返回 None。
    """
    if not raw_channel:
        return None
    try:
        return NotificationChannel(str(raw_channel))
    except ValueError:
        return None


def _snapshot_from_write(record: DownloadPendingWrite) -> DownloadPendingSnapshot:
    """
    将待写入对象投影为只读快照，供数据库瞬时不可用时写入进程内降级缓存。
    """
    return DownloadPendingSnapshot(
        pending_key=record.pending_key,
        target_key=record.target_key,
        submitted_at=record.submitted_at,
        expires_at=record.expires_at,
        media_type=record.media_type,
        title=record.title,
        year=record.year,
        media_source=record.media_source,
        media_id=record.media_id,
        season=record.season,
        episodes=record.episodes,
        downloader=record.downloader,
        hashes=record.hashes,
        torrent_name=record.torrent_name,
        source=record.source,
        subscription_id=record.subscription_id,
        payload=dict(record.payload) if record.payload is not None else None,
        last_checked_at=record.last_checked_at,
    )


def _thaw_dict(value: Any) -> Any:
    """
    把只读映射或元组转换回可变字典和列表，供 Context 与 JSON 序列化复用。
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw_dict(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_dict(item) for item in value]
    return value


class DownloadPendingOwner(_DownloadOwnerBase):
    """
    待确认下载提交的持久化记录、复核结算与目标释放 owner。
    """

    def _pending_repository(self) -> Optional[DownloadPendingRepository]:
        """
        返回已装配的待确认下载仓储；未装配的轻量测试实例返回 None。
        """
        repository = getattr(self, "download_pending_repository", None)
        return cast(Optional[DownloadPendingRepository], repository)

    @staticmethod
    def _build_pending_target_key(media: Optional[MediaInfo | MusicInfo]) -> str:
        """
        生成媒体级目标键，暴露给同 Facade 下其他下载 owner 复用。
        """
        return _build_pending_target_key(media)

    def _serialize_pending_payload(
        self,
        *,
        prepared: Any,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        userid: Union[str, int, None],
        username: Optional[str],
        custom_words: Optional[str],
        downloader: Optional[str],
        user_specified: bool,
    ) -> dict[str, Any]:
        """
        构造确认转正结算所需的脱敏快照，排除 Cookie、Passkey 与下载地址。
        """
        media = prepared.media
        meta = prepared.meta
        torrent = prepared.torrent
        media_source, media_id = resolve_media_identity(media=media)
        media_extensions = (
            self.runtime_config.media_extensions
            if getattr(self, "_runtime_config", None) is not None
            or getattr(self, "_runtime_config_provider", None) is not None
            else ()
        )
        filtered_files = [
            item
            for item in (prepared.file_list or [])
            if not media_extensions or Path(str(item)).suffix.lower() in media_extensions
        ]
        seasons = (
            sorted({int(item) for item in (meta.season_list or []) if isinstance(item, int)})
            if getattr(media, "type", None) == MediaType.TV
            else []
        )
        episode_selection = (
            torrent_rules.episode_selection_for_context(context)
            if episodes
            else None
        )
        music_track_keys = getattr(context, "music_track_keys", None)
        return {
            "media": {
                "type": media.type.value if getattr(media, "type", None) else None,
                "title": getattr(media, "title", None),
                "year": str(media.year) if getattr(media, "year", None) is not None else None,
                "media_source": media_source.value if media_source else None,
                "media_id": media_id,
                "music_type": getattr(media, "music_type", None),
                "category": getattr(media, "category", None),
                "episode_group": getattr(media, "episode_group", None),
                "backdrop_path": media.get_backdrop_image() if hasattr(media, "get_backdrop_image") else None,
                "poster_path": media.get_poster_image() if hasattr(media, "get_poster_image") else None,
            },
            "meta": {
                "org_string": getattr(meta, "org_string", None),
                "season": getattr(meta, "season", None),
                "episode": getattr(meta, "episode", None),
                "season_list": list(getattr(meta, "season_list", None) or []),
                "episode_list": list(getattr(meta, "episode_list", None) or []),
            },
            "torrent": {
                "site": getattr(torrent, "site", None),
                "site_name": getattr(torrent, "site_name", None),
                "title": getattr(torrent, "title", None),
                "description": getattr(torrent, "description", None),
                "size": getattr(torrent, "size", None),
                "pri_order": getattr(torrent, "pri_order", 0),
            },
            "download_dir": prepared.download_dir.as_posix(),
            "folder_name": prepared.folder_name,
            "file_list": filtered_files,
            "download_episodes": prepared.download_episodes,
            "episodes": sorted(episodes) if episodes else None,
            "seasons": seasons,
            "episode_selection": _serialize_episode_selection(episode_selection),
            "custom_words": custom_words,
            "username": username,
            "userid": userid,
            "channel": channel.value if channel else None,
            "subscription_download": context.subscription_download,
            "origin_media": context.origin_media,
            "downloader_scope": self._download_scope(downloader, torrent),
            "user_specified": user_specified,
            "confirmed_full_coverage": bool(getattr(context, "confirmed_full_coverage", False)),
            "selected_episodes": list(getattr(context, "selected_episodes", None) or []),
            "music_track_keys": list(music_track_keys) if music_track_keys else None,
        }

    def _record_unconfirmed_download(
        self,
        *,
        prepared: Any,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
        custom_words: Optional[str],
        actual_downloader: Optional[str],
        error_msg: str,
        user_specified: bool = False,
    ) -> Optional[DownloadPendingSnapshot]:
        """
        将“添加已受理但任务尚未确认”的提交记入待确认状态，并阻止同目标自动换源。

        不将该状态写入普通失败冷却，避免上层或后续轮次把它当成“未提交”而换下其他版本。
        """
        media = prepared.media
        meta = prepared.meta
        torrent = prepared.torrent
        target_key = self._build_pending_target_key(media)
        task_hashes = torrent_rules.download_hashes(prepared.torrent_content)
        season: Optional[int] = None
        if getattr(media, "type", None) == MediaType.TV:
            seasons = getattr(meta, "season_list", None) or []
            if len(seasons) == 1:
                season = int(seasons[0])
            elif getattr(media, "season", None) is not None and not seasons:
                season = int(media.season)
        effective_episodes: Optional[Set[int]] = None
        if episodes:
            effective_episodes = set(episodes)
        elif getattr(media, "type", None) == MediaType.TV and getattr(meta, "episode_list", None):
            effective_episodes = set(meta.episode_list)
        now_ts = time.time()
        submitted_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))
        expires_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now_ts + DOWNLOAD_PENDING_CONFIRM_TTL_SECONDS),
        )
        resolved_downloader = str(actual_downloader or downloader or prepared.site_downloader or "")
        snapshot: Optional[DownloadPendingSnapshot] = None
        if target_key:
            resource_key = self._torrent_resource_key(torrent)
            pending_key = _build_pending_key(
                target_key=target_key,
                season=season,
                episodes=effective_episodes,
                hashes=task_hashes,
                resource_key=resource_key,
            )
            media_source, media_id = resolve_media_identity(media=media)
            payload = self._serialize_pending_payload(
                prepared=prepared,
                context=context,
                episodes=episodes,
                channel=channel,
                userid=userid,
                username=username,
                custom_words=custom_words,
                downloader=downloader,
                user_specified=user_specified,
            )
            write = DownloadPendingWrite(
                pending_key=pending_key,
                target_key=target_key,
                submitted_at=submitted_at,
                expires_at=expires_at,
                media_type=media.type.value if getattr(media, "type", None) else None,
                title=getattr(media, "title", None),
                year=str(media.year) if getattr(media, "year", None) is not None else None,
                media_source=media_source,
                media_id=media_id,
                season=season,
                episodes=tuple(sorted(effective_episodes)) if effective_episodes else None,
                downloader=resolved_downloader,
                hashes=tuple(sorted(task_hashes)),
                torrent_name=getattr(torrent, "title", None),
                source=str(source)[:1000] if source else None,
                subscription_id=_parse_subscription_id(source),
                payload=payload,
            )
            snapshot = _snapshot_from_write(write)
            self._persist_pending_write(write, snapshot)

        logger.warning(
            f"{media.title_year} 下载请求已被下载器受理，但暂未确认任务信息："
            f"{torrent.title}（{error_msg}），已保留待确认状态并暂停同目标自动换源，后续将按种子标识重新核对"
        )
        self.post_message(
            Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"下载任务待确认：{media.title_year} {meta.season_episode}",
                text=(
                    f"站点：{torrent.site_name}\n"
                    f"种子名称：{meta.org_string}\n"
                    f"状态说明：{error_msg}\n"
                    "已保留待确认状态并暂停自动提交其他版本，系统将在后续自动核对下载器中的任务状态。"
                ),
                image=media.get_message_image() if hasattr(media, "get_message_image") else None,
                poster=media.get_poster_image() or None if hasattr(media, "get_poster_image") else None,
                userid=userid,
            )
        )
        return snapshot

    def _persist_pending_write(
        self,
        write: DownloadPendingWrite,
        snapshot: DownloadPendingSnapshot,
    ) -> None:
        """
        优先写入持久化仓储；仓储异常时退回进程内兜底缓存，避免数据库锁导致丢状态。
        """
        repository = self._pending_repository()
        if repository is None:
            return
        try:
            repository.upsert(write)
            with _FALLBACK_PENDING_LOCK:
                _FALLBACK_PENDING_RECORDS.pop(write.pending_key, None)
        except Exception as err:
            logger.error(f"保存待确认下载状态失败，改用进程内临时兜底：{err}")
            with _FALLBACK_PENDING_LOCK:
                _FALLBACK_PENDING_RECORDS[write.pending_key] = snapshot

    def list_pending_downloads(
        self,
        *,
        target_keys: Optional[list[str]] = None,
        subscription_id: Optional[int] = None,
    ) -> list[DownloadPendingSnapshot]:
        """
        只读查询待确认下载记录，不向下载器发起网络请求。

        :param target_keys: 可选的媒体目标键筛选列表
        :param subscription_id: 可选的订阅主键筛选条件
        :return: 匹配的待确认记录快照列表
        """
        records: dict[str, DownloadPendingSnapshot] = {}
        repository = self._pending_repository()
        if repository is not None:
            try:
                if target_keys is not None:
                    fetched = repository.list_by_target_keys(target_keys)
                elif subscription_id is not None:
                    fetched = repository.list_by_subscription_id(subscription_id)
                else:
                    fetched = repository.list_all()
                for item in fetched:
                    records[item.pending_key] = item
            except Exception as err:
                logger.error(f"查询待确认下载状态失败：{err}")
        with _FALLBACK_PENDING_LOCK:
            for key, item in _FALLBACK_PENDING_RECORDS.items():
                if target_keys is not None and item.target_key not in target_keys:
                    continue
                if subscription_id is not None and item.subscription_id != subscription_id:
                    continue
                records.setdefault(key, item)
        return list(records.values())

    def clear_pending_downloads_for_media(self, media: Optional[MediaInfo | MusicInfo]) -> int:
        """
        媒体已确认入库完成时清理该目标关联的待确认记录。

        :param media: 已完成入库的媒体对象
        :return: 清理的记录数量
        """
        target_key = self._build_pending_target_key(media)
        if not target_key:
            return 0
        deleted = 0
        repository = self._pending_repository()
        if repository is not None:
            try:
                deleted += repository.delete_by_target_key(target_key)
            except Exception as err:
                logger.error(f"清理待确认下载状态失败：{err}")
        with _FALLBACK_PENDING_LOCK:
            fallback_keys = [
                key for key, item in _FALLBACK_PENDING_RECORDS.items()
                if item.target_key == target_key
            ]
            for key in fallback_keys:
                _FALLBACK_PENDING_RECORDS.pop(key, None)
                deleted += 1
        return deleted

    def reconcile_pending_downloads(
        self,
        *,
        target_keys: Optional[list[str]] = None,
        subscription_id: Optional[int] = None,
    ) -> PendingReconciliationResult:
        """
        按已知种子标识向下载器重新核对待确认提交，并分类执行转正结算、续保或释放。

        待确认记录为空时直接返回空结果，不向下载器发起任何请求。

        :param target_keys: 可选的媒体目标键筛选列表
        :param subscription_id: 可选的订阅主键筛选条件
        :return: 本轮核对的已确认、仍待确认和已释放记录汇总
        """
        candidates = self.list_pending_downloads(
            target_keys=target_keys,
            subscription_id=subscription_id,
        )
        if not candidates:
            return PendingReconciliationResult()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        confirmed: list[ConfirmedPendingDownload] = []
        still_pending: list[DownloadPendingSnapshot] = []
        released: list[DownloadPendingSnapshot] = []
        for record in candidates:
            outcome, confirmed_item = self._reconcile_single_pending(record, now_time)
            if outcome == "confirmed" and confirmed_item is not None:
                confirmed.append(confirmed_item)
            elif outcome == "released":
                released.append(record)
            elif outcome == "pending":
                still_pending.append(record)
        return PendingReconciliationResult(
            confirmed=tuple(confirmed),
            still_pending=tuple(still_pending),
            released=tuple(released),
        )

    def _reconcile_single_pending(
        self,
        record: DownloadPendingSnapshot,
        now_time: str,
    ) -> tuple[str, Optional[ConfirmedPendingDownload]]:
        """
        在任务互斥锁内完成单条待确认记录的复核、幂等转正或超期释放。
        """
        lock_keys: Iterable[str] = record.hashes or (record.pending_key,)
        with downloader_task_locks.hold(lock_keys):
            current = self._reload_pending_record(record)
            if current is None:
                # 已被并发线程确认结算或释放，无需重复处理。
                return "already_gone", None
            settled_hash = self._find_settled_history_for_pending(current)
            if settled_hash is not None:
                self._delete_pending_record(current.pending_key)
                context = self._build_confirmed_context(current)
                return "confirmed", ConfirmedPendingDownload(
                    record=current,
                    context=context,
                    downloader=current.downloader,
                    download_hash=settled_hash,
                )
            check = self._query_pending_in_downloader(current)
            if check.status is PendingDownloadCheckStatus.CONFIRMED and check.download_hash:
                confirmed_item = self._settle_confirmed_pending(current, check)
                return "confirmed", confirmed_item
            if check.status is PendingDownloadCheckStatus.REJECTED:
                self._delete_pending_record(current.pending_key)
                logger.warning(
                    f"待确认下载任务核对后被下载器拒绝，已释放保护：{current.torrent_name}（{check.message}）"
                )
                return "released", None
            if check.status is PendingDownloadCheckStatus.NOT_FOUND and now_time >= current.expires_at:
                self._delete_pending_record(current.pending_key)
                logger.warning(
                    f"待确认下载任务在保护期（截止 {current.expires_at}）内未出现在下载器中，"
                    f"已释放保护并允许重新尝试：{current.torrent_name}"
                )
                return "released", None
            self._touch_pending_checked(current.pending_key, now_time)
            return "pending", None

    def _reload_pending_record(self, record: DownloadPendingSnapshot) -> Optional[DownloadPendingSnapshot]:
        """
        持锁后二次读取待确认记录，防止并发核对重复结算。
        """
        repository = self._pending_repository()
        if repository is not None:
            try:
                loaded = repository.get_by_key(record.pending_key)
                if loaded is not None:
                    return loaded
            except Exception as err:
                logger.error(f"复核读取待确认下载状态失败：{err}")
        with _FALLBACK_PENDING_LOCK:
            return _FALLBACK_PENDING_RECORDS.get(record.pending_key)

    def _delete_pending_record(self, pending_key: str) -> None:
        """
        删除已确认或已释放的待确认记录，并同步清理降级缓存。
        """
        repository = self._pending_repository()
        if repository is not None:
            try:
                repository.delete_by_key(pending_key)
            except Exception as err:
                logger.error(f"删除待确认下载状态失败：{err}")
        with _FALLBACK_PENDING_LOCK:
            _FALLBACK_PENDING_RECORDS.pop(pending_key, None)

    def _touch_pending_checked(self, pending_key: str, checked_at: str) -> None:
        """
        记录一次未命中核对时间，不延长保护窗口。
        """
        repository = self._pending_repository()
        if repository is not None:
            try:
                repository.touch_checked(pending_key, checked_at)
            except Exception as err:
                logger.debug(f"更新待确认下载核对时间失败：{err}")

    def _find_settled_history_for_pending(
        self,
        record: DownloadPendingSnapshot,
    ) -> Optional[str]:
        """
        仅使用同一下载器实例在提交时或之后写入的历史作为已结算凭据。

        :param record: 待恢复的提交记录；缺少实例名称时继续向下载器核对
        :return: 符合恢复条件的任务哈希；无匹配时返回 None
        """
        if not record.downloader or not record.hashes:
            return None
        history_repo = self.download_history_repository
        if history_repo is None:
            return None
        candidates = sorted({variant for item in record.hashes for variant in (item.lower(), item.upper())})
        try:
            histories = history_repo.get_by_hashes(candidates, downloader=record.downloader)
        except Exception as err:
            logger.debug(f"幂等检查待确认下载历史失败：{err}")
            return None
        for history_hash, item in histories.items():
            if item.date and item.date >= record.submitted_at:
                return history_hash
        return None

    def _query_pending_in_downloader(
        self,
        record: DownloadPendingSnapshot,
    ) -> PendingDownloadCheckResult:
        """
        调用下载器核对能力检查待确认哈希；无法解析哈希或读取历史失败时保守处理。
        """
        if not record.hashes:
            return PendingDownloadCheckResult(
                status=PendingDownloadCheckStatus.NOT_FOUND,
                downloader=record.downloader or None,
            )
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        raw_episodes = payload.get("episodes")
        episodes = (
            {int(item) for item in raw_episodes if isinstance(item, int) and not isinstance(item, bool)}
            if isinstance(raw_episodes, (list, tuple)) and raw_episodes
            else None
        )
        try:
            previous_selection = self._existing_task_confirmed_selection(frozenset(record.hashes))
        except Exception as err:
            logger.warning(f"核对待确认任务时读取下载历史失败，继续保持待确认：{err}")
            return PendingDownloadCheckResult(
                status=PendingDownloadCheckStatus.QUERY_ERROR,
                downloader=record.downloader or None,
                message=f"读取下载历史失败：{err}",
            )
        custom_words = payload.get("custom_words") if isinstance(payload.get("custom_words"), str) else None
        episode_selection = _deserialize_episode_selection(payload.get("episode_selection"))
        user_specified = bool(payload.get("user_specified", False))
        try:
            result = self.confirm_pending_download(
                hashes=list(record.hashes),
                downloader=record.downloader or None,
                episodes=episodes,
                custom_words=custom_words,
                episode_selection=episode_selection,
                confirmed_selection=previous_selection,
                user_specified=user_specified,
            )
        except Exception as err:
            logger.warning(f"核对待确认任务时调用下载器失败，继续保持待确认：{err}")
            return PendingDownloadCheckResult(
                status=PendingDownloadCheckStatus.QUERY_ERROR,
                downloader=record.downloader or None,
                message=str(err),
            )
        if result is not None:
            return result
        # 第三方插件下载器未实现 confirm_pending_download 时，降级通过 list_torrents 按哈希查询。
        return self._fallback_confirm_via_list_torrents(record)

    def _fallback_confirm_via_list_torrents(
        self,
        record: DownloadPendingSnapshot,
    ) -> PendingDownloadCheckResult:
        """
        为未实现 ``confirm_pending_download`` 的第三方下载器提供按哈希查询兜底。
        """
        try:
            torrents = self.list_torrents(
                hashs=list(record.hashes),
                downloader=record.downloader or None,
                include_all_tags=True,
            )
        except Exception as err:
            return PendingDownloadCheckResult(
                status=PendingDownloadCheckStatus.QUERY_ERROR,
                downloader=record.downloader or None,
                message=str(err),
            )
        if torrents is None:
            return PendingDownloadCheckResult(
                status=PendingDownloadCheckStatus.QUERY_ERROR,
                downloader=record.downloader or None,
            )
        expected = {item.lower() for item in record.hashes}
        for torrent in torrents:
            torrent_hash = str(getattr(torrent, "hash", "") or "")
            if torrent_hash.lower() in expected:
                return PendingDownloadCheckResult(
                    status=PendingDownloadCheckStatus.CONFIRMED,
                    downloader=getattr(torrent, "downloader", None) or record.downloader or None,
                    download_hash=torrent_hash,
                    layout="Original",
                    message="添加下载成功",
                )
        return PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.NOT_FOUND,
            downloader=record.downloader or None,
        )

    def _build_confirmed_context(self, record: DownloadPendingSnapshot) -> Context:
        """
        按待确认记录的脱敏快照重建用于订阅进度与完成结算的最小 Context。
        """
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        media = _restore_media_from_record(record, payload)
        meta = _restore_meta_from_record(record, payload, media)
        torrent = _restore_torrent_from_record(record, payload)
        context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
        raw_sub_download = payload.get("subscription_download")
        if isinstance(raw_sub_download, Mapping):
            context.subscription_download = _thaw_dict(raw_sub_download)
        raw_origin_media = payload.get("origin_media")
        if isinstance(raw_origin_media, Mapping):
            context.origin_media = _thaw_dict(raw_origin_media)
        raw_selected = payload.get("selected_episodes")
        if isinstance(raw_selected, (list, tuple)):
            context.selected_episodes = [
                int(item) for item in raw_selected if isinstance(item, int) and not isinstance(item, bool)
            ]
        elif record.episodes:
            context.selected_episodes = list(record.episodes)
        context.confirmed_full_coverage = bool(payload.get("confirmed_full_coverage", False))
        raw_tracks = payload.get("music_track_keys")
        if isinstance(raw_tracks, (list, tuple)):
            context.music_track_keys = [str(item) for item in raw_tracks if item]
        return context

    def _settle_confirmed_pending(
        self,
        record: DownloadPendingSnapshot,
        check: PendingDownloadCheckResult,
    ) -> ConfirmedPendingDownload:
        """
        将已在下载器确认的待确认记录补齐下载历史与事件，然后删除待确认状态。
        """
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        context = self._build_confirmed_context(record)
        assert context.media_info is not None and context.meta_info is not None and context.torrent_info is not None
        raw_episodes = payload.get("episodes")
        episodes = (
            {int(item) for item in raw_episodes if isinstance(item, int) and not isinstance(item, bool)}
            if isinstance(raw_episodes, (list, tuple)) and raw_episodes
            else None
        )
        custom_words = payload.get("custom_words") if isinstance(payload.get("custom_words"), str) else None
        raw_dir = str(payload.get("download_dir") or "")
        fallback_dir = Path(raw_dir) if raw_dir else Path(".")
        download_dir = self._resolve_actual_download_dir(
            downloader=check.downloader,
            download_hash=cast(str, check.download_hash),
            fallback=fallback_dir,
        )
        raw_files = payload.get("file_list")
        file_list = [str(item) for item in raw_files] if isinstance(raw_files, (list, tuple)) else []
        try:
            previous_selection = self._existing_task_confirmed_selection(frozenset(record.hashes))
        except Exception:
            previous_selection = None
        accepted_files = self._accepted_task_files(cast(str, check.download_hash), check.downloader)
        task_action = DownloadTaskAction.from_downloader_message(check.message)
        self._settle_download_success(
            context=context,
            media=context.media_info,
            meta=context.meta_info,
            torrent=context.torrent_info,
            folder_name=str(payload.get("folder_name") or ""),
            file_list=file_list,
            download_dir=download_dir,
            layout=check.layout,
            downloader=check.downloader,
            download_hash=cast(str, check.download_hash),
            download_episodes=payload.get("download_episodes") if isinstance(payload.get("download_episodes"), str) else None,
            episodes=episodes,
            channel=_restore_channel(payload.get("channel")),
            source=record.source,
            userid=payload.get("userid"),
            username=payload.get("username") if isinstance(payload.get("username"), str) else None,
            torrent_content=b"",
            custom_words=custom_words,
            task_action=task_action,
            previous_selection=previous_selection,
            accepted_files=accepted_files,
            downloader_scope=str(payload.get("downloader_scope") or record.downloader or ""),
        )
        self._delete_pending_record(record.pending_key)
        logger.info(
            f"待确认下载任务已在下载器确认：{record.torrent_name}（下载器={check.downloader}，"
            f"hash={check.download_hash}），已补齐下载历史并清除待确认状态"
        )
        return ConfirmedPendingDownload(
            record=record,
            context=context,
            downloader=check.downloader,
            download_hash=cast(str, check.download_hash),
        )
