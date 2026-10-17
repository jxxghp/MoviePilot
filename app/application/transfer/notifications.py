"""
整理结果通知的分组、批次等待和短暂聚合。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from app.application.transfer.feedback import TransferFailureNotification
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo

if TYPE_CHECKING:
    from app.application.transfer.models import TransferTask


def build_transfer_failure_group_key(task: TransferTask) -> str:
    """
    构造主程序和第三方整理路径可共同使用的失败通知分组键。
    """
    media_source, media_id = resolve_media_identity(media=task.mediainfo)
    if not media_source or not media_id:
        media_source, media_id = resolve_media_identity(media=task)
    season = getattr(task.meta, "begin_season", None) if task.meta else None
    username = task.username or ""
    if media_source and media_id:
        return f"media:{media_source}:{media_id}:season:{season}:user:{username}"
    if task.download_hash:
        return f"download:{task.download_hash}:user:{username}"
    source_path = str(task.fileitem.path) if task.fileitem else ""
    parent_path = str(Path(source_path).parent) if source_path else ""
    return f"path:{parent_path or source_path}:user:{username}"


@dataclass(frozen=True, slots=True)
class TransferSuccessNotification:
    """
    保存逐任务成功结果副本，避免后续作业统计修改已缓冲通知。

    :param meta: 用于通知展示季集和发布名的已解析元数据
    :param mediainfo: 用于标题、海报和来源链接的标准媒体信息
    :param transferinfo: 文件整理结果，包括目录、大小与方式
    :param episodes_info: 通知可使用的剧集详情；没有补充信息时为 None
    :param username: 触发整理的本地用户名；未绑定时为 None
    """

    meta: MetaBase
    mediainfo: MediaInfo | MusicInfo
    transferinfo: TransferInfo
    episodes_info: Optional[list[TmdbEpisode]]
    username: Optional[str]


def build_transfer_success_group_key(task: TransferTask, result: TransferInfo) -> str:
    """
    按下载来源、媒体目录、季和用户隔离成功通知，避免跨专辑或用户混发。
    """
    directory = result.target_diritem
    destination = (directory.storage, directory.path) if directory else (task.target_storage, str(task.target_path))
    source = (task.downloader, task.download_hash) if task.download_hash else (task.fileitem.storage, str(Path(task.fileitem.path or "").parent))
    season = getattr(task.meta, "begin_season", None)
    return repr((source, destination, season, task.username))


class TransferNotificationAggregator[Notification]:
    """
    在线程安全缓冲中等待明确批次结束，或按静默窗口合并通知。
    """

    NOTIFICATION_DEBOUNCE_SECONDS = 30

    def __init__(self) -> None:
        """
        初始化分组缓冲、回调、定时器与关闭状态。
        """
        self._buffers: dict[str, list[Notification]] = {}
        self._callbacks: dict[
            str,
            Callable[[list[Notification]], None],
        ] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._generations: dict[str, int] = {}
        self._next_generation = 0
        self._waiting_batches: dict[str, set[str]] = {}
        self._debounce_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()
        self._closed = False

    def schedule(
            self,
            *,
            group_key: str,
            notification: Notification,
            callback: Callable[[list[Notification]], None],
            loop: Optional[asyncio.AbstractEventLoop] = None,
            defer_until: Optional[str] = None,
    ) -> None:
        """
        先登记快照；明确批次等待完成信号，其余通知使用事件循环静默窗口。
        """
        # 先在调用线程登记快照，关闭流程才能覆盖已接收但尚未进入事件循环的通知。
        with self._lock:
            if self._closed:
                raise RuntimeError("整理通知聚合器正在关闭，不能再接收通知")
            self._buffers.setdefault(group_key, []).append(notification)
            self._callbacks[group_key] = callback
            # 分组刷新后也不复用代号，避免旧回调命中新创建的同名分组。
            self._next_generation += 1
            generation = self._next_generation
            self._generations[group_key] = generation
            if defer_until:
                self._waiting_batches.setdefault(group_key, set()).add(defer_until)
            if loop is not None:
                self._debounce_loops[group_key] = loop
        if defer_until:
            return
        self._schedule_flush(group_key, generation, callback, loop)

    def _schedule_flush(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[Notification]], None],
            loop: Optional[asyncio.AbstractEventLoop],
    ) -> None:
        """
        统一投递静默窗口，事件循环不可用时回退为立即发送。
        """
        if loop is None:
            self.flush(group_key, generation, callback)
            return
        try:
            loop.call_soon_threadsafe(
                self._schedule_on_loop,
                group_key,
                generation,
                callback,
                loop,
            )
        except Exception as err:
            logger.error(
                f"创建整理通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {err}"
            )
            self.flush(group_key, generation, callback)

    def _schedule_on_loop(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[Notification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        在所属事件循环中为已登记缓冲重置静默窗口。
        """
        schedule_error: Exception | None = None
        with self._lock:
            if (
                    self._closed
                    or group_key not in self._buffers
                    or self._generations.get(group_key) != generation
                    or self._waiting_batches.get(group_key)
            ):
                return
            timer = self._timers.pop(group_key, None)
            if timer:
                timer.cancel()
            try:
                self._timers[group_key] = loop.call_later(
                    self.NOTIFICATION_DEBOUNCE_SECONDS,
                    self.flush,
                    group_key,
                    generation,
                    callback,
                )
            except Exception as err:
                schedule_error = err
        if schedule_error is not None:
            logger.error(
                f"创建整理通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {schedule_error}"
            )
            self.flush(group_key, generation, callback)

    def flush(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[Notification]], None],
    ) -> None:
        """
        发送一个分组内的聚合结果并释放缓冲。
        """
        with self._lock:
            # 新通知已登记但 timer 重置回调尚未执行时，旧代不得提前发送新批次。
            if self._generations.get(group_key) != generation or self._waiting_batches.get(group_key):
                return
            notifications = self._buffers.pop(group_key, [])
            self._callbacks.pop(group_key, None)
            timer = self._timers.pop(group_key, None)
            self._generations.pop(group_key, None)
            self._waiting_batches.pop(group_key, None)
            self._debounce_loops.pop(group_key, None)
        if timer:
            timer.cancel()
        if not notifications:
            return
        self._deliver(group_key, notifications, callback)

    def release_batch(self, batch_id: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """
        批次终结后释放等待；传入循环时合并后续文件，同组仍须等待其他批次。
        """
        with self._lock:
            ready = []
            for group_key, batches in self._waiting_batches.items():
                if batch_id not in batches:
                    continue
                batches.remove(batch_id)
                if loop is not None:
                    self._debounce_loops[group_key] = loop
                if not batches:
                    ready.append((group_key, self._generations[group_key], self._callbacks[group_key],
                                  self._debounce_loops.get(group_key)))
        for group_key, generation, callback, debounce_loop in ready:
            self._schedule_flush(group_key, generation, callback, debounce_loop)

    @staticmethod
    def _deliver(
            group_key: str,
            notifications: list[Notification],
            callback: Callable[[list[Notification]], None],
    ) -> None:
        """
        调用聚合通知回调，并统一观察发送异常。
        """
        try:
            callback(notifications)
        except Exception as err:
            logger.error(f"发送整理聚合通知失败 (group={group_key}): {err}")

    def close(self) -> None:
        """
        停止接收新通知，取消定时器并同步发送全部已缓冲通知。
        """
        with self._lock:
            if self._closed and not self._buffers and not self._timers:
                return
            self._closed = True
            timers = list(self._timers.values())
            pending = []
            orphaned = []
            for group_key, notifications in self._buffers.items():
                callback = self._callbacks.get(group_key)
                if callback is None:
                    orphaned.append((group_key, len(notifications)))
                    continue
                pending.append((group_key, notifications, callback))
            self._timers.clear()
            self._generations.clear()
            self._buffers.clear()
            self._callbacks.clear()
            self._waiting_batches.clear()
            self._debounce_loops.clear()

        for timer in timers:
            timer.cancel()
        for group_key, notification_count in orphaned:
            logger.error(
                f"整理通知聚合缓冲缺少发送回调，无法刷新 "
                f"(group={group_key}, count={notification_count})"
            )
        for group_key, notifications, callback in pending:
            self._deliver(group_key, notifications, callback)


class TransferFailureNotificationAggregator(TransferNotificationAggregator[TransferFailureNotification]):
    """
    保留失败通知已有入口及三十秒静默聚合行为。
    """
