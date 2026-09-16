"""整理失败通知的分组键和短暂聚合。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.application.transfer.feedback import TransferFailureNotification
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity

if TYPE_CHECKING:
    from app.application.transfer.models import TransferTask


def build_transfer_failure_group_key(task: TransferTask) -> str:
    """构造主程序和第三方整理路径可共同使用的失败通知分组键。"""
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


class TransferFailureNotificationAggregator:
    """在短暂静默窗口内按媒体合并整理失败通知。"""

    NOTIFICATION_DEBOUNCE_SECONDS = 30

    def __init__(self) -> None:
        """初始化分组缓冲、回调、定时器与关闭状态。"""
        self._buffers: dict[str, list[TransferFailureNotification]] = {}
        self._callbacks: dict[
            str,
            Callable[[list[TransferFailureNotification]], None],
        ] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._generations: dict[str, int] = {}
        self._lock = threading.Lock()
        self._closed = False

    def schedule(
            self,
            *,
            group_key: str,
            notification: TransferFailureNotification,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """从整理线程安全地把失败快照加入事件循环中的聚合缓冲。"""
        # 先在调用线程登记快照，关闭流程才能覆盖已接收但尚未进入事件循环的通知。
        with self._lock:
            if self._closed:
                raise RuntimeError("整理失败通知聚合器正在关闭，不能再接收通知")
            self._buffers.setdefault(group_key, []).append(notification)
            self._callbacks[group_key] = callback
            generation = self._generations.get(group_key, 0) + 1
            self._generations[group_key] = generation
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
                f"创建整理失败通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {err}"
            )
            self.flush(group_key, generation, callback)

    def _schedule_on_loop(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """在所属事件循环中为已登记缓冲重置静默窗口。"""
        schedule_error: Exception | None = None
        with self._lock:
            if (
                    self._closed
                    or group_key not in self._buffers
                    or self._generations.get(group_key) != generation
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
                f"创建整理失败通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {schedule_error}"
            )
            self.flush(group_key, generation, callback)

    def flush(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[TransferFailureNotification]], None],
    ) -> None:
        """发送一个分组内的聚合结果并释放缓冲。"""
        with self._lock:
            # 新通知已登记但 timer 重置回调尚未执行时，旧代不得提前发送新批次。
            if self._generations.get(group_key) != generation:
                return
            notifications = self._buffers.pop(group_key, [])
            self._callbacks.pop(group_key, None)
            timer = self._timers.pop(group_key, None)
            self._generations.pop(group_key, None)
        if timer:
            timer.cancel()
        if not notifications:
            return
        self._deliver(group_key, notifications, callback)

    @staticmethod
    def _deliver(
            group_key: str,
            notifications: list[TransferFailureNotification],
            callback: Callable[[list[TransferFailureNotification]], None],
    ) -> None:
        """调用聚合通知回调，并统一观察发送异常。"""
        try:
            callback(notifications)
        except Exception as err:
            logger.error(f"发送整理失败聚合通知失败 (group={group_key}): {err}")

    def close(self) -> None:
        """停止接收新通知，取消定时器并同步发送全部已缓冲通知。"""
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

        for timer in timers:
            timer.cancel()
        for group_key, notification_count in orphaned:
            logger.error(
                f"整理失败通知聚合缓冲缺少发送回调，无法刷新 "
                f"(group={group_key}, count={notification_count})"
            )
        for group_key, notifications, callback in pending:
            self._deliver(group_key, notifications, callback)
