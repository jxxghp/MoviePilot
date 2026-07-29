import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from watchfiles import Change, DefaultFilter, watch

from app.log import logger


@dataclass(frozen=True)
class DirectoryChangeEvent:
    """
    目录文件变化事件，隔离底层 watchfiles 事件结构。
    """
    change_type: Change
    src_path: str
    is_directory: bool


class LocalDirectoryWatcher:
    """
    基于 watchfiles 的本地目录监控线程。
    """
    _HANDLE_CHANGES = {Change.added, Change.modified}
    # 监控循环异常退出后的重启退避秒数，网络存储/FUSE 挂载抖动通常是暂时的
    RESTART_BACKOFF = (5, 15, 30, 60, 120, 300)
    # 单次监控循环存活超过该秒数视为已恢复，重置退避
    HEALTHY_UPTIME = 60
    # 超过该秒数监控循环没有任何活动，判定为静默失效
    STALL_TIMEOUT = 600
    # 轮询模式目录扫描间隔（毫秒）：本地磁盘用 watchfiles 默认值
    POLL_DELAY_LOCAL_MS = 300
    # 网络/FUSE 挂载轮询降频，减少监控自身对挂载后端的持续 stat 压力
    POLL_DELAY_NETWORK_MS = 5000

    def __init__(self, mon_path: Path, callback: Any, force_polling: Optional[bool] = None,
                 poll_delay_ms: Optional[int] = None):
        """
        初始化本地目录监控。
        :param mon_path: 监控目录
        :param callback: 目录变化回调对象
        :param force_polling: 是否强制使用轮询模式，None 表示由 watchfiles 自动选择
        :param poll_delay_ms: 轮询模式目录扫描间隔（毫秒），仅轮询时生效
        """
        self._watch_path = mon_path
        self._callback = callback
        self._force_polling = force_polling
        self._poll_delay_ms = poll_delay_ms or self.POLL_DELAY_LOCAL_MS
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watch_filter = DefaultFilter()
        # 最近一次监控循环活动时间（monotonic），用于检测静默失效
        self._last_activity: float = 0.0
        # 累计自动重启次数
        self._restart_count: int = 0

    @property
    def watch_path(self) -> Path:
        """
        获取监控目录。
        :return: 监控目录
        """
        return self._watch_path

    @property
    def force_polling(self) -> Optional[bool]:
        """
        获取监控模式配置，重建监控线程时沿用。
        :return: 是否强制轮询
        """
        return self._force_polling

    @property
    def restart_count(self) -> int:
        """
        获取累计自动重启次数。
        :return: 自动重启次数
        """
        return self._restart_count

    @property
    def poll_delay_ms(self) -> int:
        """
        获取轮询模式目录扫描间隔（毫秒），重建监控线程时沿用。
        :return: 扫描间隔
        """
        return self._poll_delay_ms

    def start(self):
        """
        启动本地目录监控线程。
        """
        if not self._watch_path.exists():
            raise FileNotFoundError(f"监控目录不存在: {self._watch_path}")
        if not self._watch_path.is_dir():
            raise NotADirectoryError(f"监控路径不是目录: {self._watch_path}")
        if self.is_alive():
            logger.info(f"本地目录监控已在运行中: {self._watch_path}")
            return
        self._stop_event.clear()
        self._mark_activity()
        self._thread = threading.Thread(
            target=self._run,
            name=f"MoviePilot-DirectoryWatcher-{self._watch_path.name}",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        """
        请求停止本地目录监控线程。
        """
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None):
        """
        等待本地目录监控线程退出。
        :param timeout: 最长等待秒数
        """
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """
        判断监控线程是否仍在运行。
        :return: 线程存活状态
        """
        return bool(self._thread and self._thread.is_alive())

    def is_stalled(self) -> bool:
        """
        判断监控线程是否已静默失效（线程存活但监控循环长时间无任何活动）。
        :return: 是否静默失效
        """
        if self._stop_event.is_set() or not self.is_alive():
            return False
        if not self._last_activity:
            return False
        return (time.monotonic() - self._last_activity) > self.STALL_TIMEOUT

    def _mark_activity(self):
        """
        记录一次监控循环活动时间，作为静默失效检测的心跳。
        """
        self._last_activity = time.monotonic()

    def _run(self):
        """
        运行 watchfiles 主循环，异常时退避重启，避免一次故障导致监控永久停摆。
        """
        # 快速模式失败后降级为轮询，降级后的失败一律走退避重启
        force_polling = self._force_polling
        attempt = 0
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                self._mark_activity()
                self._run_watch(force_polling=force_polling)
                # 正常返回表示收到停止信号
                return
            except Exception as err:
                if self._stop_event.is_set():
                    return
                # 崩溃堆栈按 ERROR 级输出，生产环境 LOG_LEVEL=ERROR 时也能落盘
                logger.error(f"本地目录监控异常堆栈: {self._watch_path}\n{traceback.format_exc()}")
                if force_polling is not True:
                    logger.warn(f"快速模式监控 {self._watch_path} 失败，将自动切换到兼容模式: {err}")
                    force_polling = True
                    continue
                if time.monotonic() - started_at >= self.HEALTHY_UPTIME:
                    # 上一轮监控已稳定运行过，重新从最短间隔开始退避
                    attempt = 0
                delay = self.RESTART_BACKOFF[min(attempt, len(self.RESTART_BACKOFF) - 1)]
                attempt += 1
                self._restart_count += 1
                logger.error(f"本地目录监控发生错误，{delay} 秒后自动重启"
                             f"（累计第 {self._restart_count} 次）: {self._watch_path} - {err}")
                if self._stop_event.wait(timeout=delay):
                    return

    def _run_watch(self, force_polling: Optional[bool]):
        """
        执行一次 watchfiles 监控循环。
        :param force_polling: 是否强制轮询
        """
        for changes in watch(
                str(self._watch_path),
                watch_filter=self._watch_filter,
                stop_event=self._stop_event,
                rust_timeout=1000,
                yield_on_timeout=True,
                force_polling=force_polling,
                poll_delay_ms=self._poll_delay_ms,
                recursive=True,
                ignore_permission_denied=True):
            self._mark_activity()
            if self._stop_event.is_set():
                break
            if not changes:
                continue
            self._handle_changes(changes)
            self._mark_activity()

    def _handle_changes(self, changes: set[tuple[Change, str]]):
        """
        将 watchfiles 原始变更转换为目录监控事件。
        :param changes: watchfiles 返回的变更集合
        """
        changes = self._expand_added_directories(changes)
        for change_type, path_str in sorted(changes, key=lambda item: item[1]):
            # 批量整理可能持续较久，逐个文件刷新心跳，避免被误判为静默失效
            self._mark_activity()
            if change_type not in self._HANDLE_CHANGES:
                continue
            event_path = Path(path_str)
            event = self._build_event(change_type=change_type, event_path=event_path)
            if not event or event.is_directory:
                continue
            file_size = self._get_file_size(event_path)
            if file_size is None:
                continue
            text = self._change_text(change_type)
            try:
                self._callback.event_handler(
                    event=event,
                    text=text,
                    event_path=path_str,
                    file_size=file_size
                )
            except Exception as err:
                logger.error(f"处理本地目录监控事件失败: {path_str} - {err}")

    def _expand_added_directories(self, changes: set[tuple[Change, str]]) -> set[tuple[Change, str]]:
        """
        将整体移入监控范围的新增目录展开为内部文件事件。
        :param changes: watchfiles 返回的变更集合
        :return: 包含目录内新增文件的变更集合
        """
        expanded_changes = set(changes)
        for change_type, path_str in changes:
            if change_type != Change.added:
                continue
            event_path = Path(path_str)
            try:
                if not event_path.is_dir():
                    continue
                for nested_path in event_path.rglob("*"):
                    if not nested_path.is_file():
                        continue
                    nested_path_str = nested_path.as_posix()
                    if self._watch_filter(Change.added, nested_path_str):
                        expanded_changes.add((Change.added, nested_path_str))
            except OSError as err:
                logger.debug(f"扫描新增目录失败: {event_path} - {err}")
        return expanded_changes

    @staticmethod
    def _build_event(change_type: Change, event_path: Path) -> Optional[DirectoryChangeEvent]:
        """
        构建目录变化事件，路径已不存在时忽略。
        :param change_type: watchfiles 变化类型
        :param event_path: 变化路径
        :return: 目录变化事件
        """
        try:
            is_directory = event_path.is_dir()
        except OSError as err:
            logger.debug(f"读取目录监控事件路径失败: {event_path} - {err}")
            return None
        if not event_path.exists():
            return None
        return DirectoryChangeEvent(
            change_type=change_type,
            src_path=event_path.as_posix(),
            is_directory=is_directory
        )

    @staticmethod
    def _get_file_size(event_path: Path) -> Optional[int]:
        """
        读取事件文件大小，文件已消失时返回 None。
        :param event_path: 事件文件路径
        :return: 文件大小
        """
        try:
            return event_path.stat().st_size
        except OSError as err:
            logger.debug(f"读取目录监控文件大小失败: {event_path} - {err}")
            return None

    @staticmethod
    def _change_text(change_type: Change) -> str:
        """
        转换 watchfiles 事件类型为日志文案。
        :param change_type: watchfiles 变化类型
        :return: 事件描述
        """
        if change_type == Change.modified:
            return "修改"
        return "新增"
