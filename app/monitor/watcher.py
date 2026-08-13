import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from watchfiles import Change, DefaultFilter, watch

from app.core.config import settings
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
    # 新增目录延迟重扫的间隔秒数默认值：FUSE 上目录内容的可见性有延迟，首次展开时
    # 看不到的文件不会再产生任何事件，只能靠延迟重扫补回。默认在常见的 30/120 秒
    # 窗口后追加两轮成本极低的长延迟轮次（600s/1800s），应对超大目录树的极端延迟；
    # 实际生效值可通过 MONITOR_RESCAN_DELAYS 配置覆盖，见 DIRECTORY_RESCAN_DELAYS
    DEFAULT_RESCAN_DELAYS = (30, 120, 600, 1800)
    # 待重扫目录队列上限，避免大批量移入时无限增长
    MAX_PENDING_RESCANS = 100
    # 单个重扫条目允许的连续「整体扫描失败」次数上限：扫描失败（如 FUSE 瞬时抖动）
    # 时条目会原地重试而不消耗重扫轮次，必须设置上限，避免目录被删除或长期不可
    # 访问时无限重试、占满队列
    MAX_RESCAN_FAILURES = 5

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
        # 最近一次活动的墙钟时间，供监控重建后的补偿扫描定位停摆起点
        self._last_activity_wall: float = 0.0
        # 累计自动重启次数
        self._restart_count: int = 0
        # 待延迟重扫的新增目录
        self._pending_rescans: list[dict] = []

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

    @property
    def last_activity_time(self) -> float:
        """
        获取最近一次监控循环活动的墙钟时间。
        :return: Unix 时间戳，从未活动过时为 0
        """
        return self._last_activity_wall

    def _mark_activity(self):
        """
        记录一次监控循环活动时间，作为静默失效检测的心跳。
        """
        self._last_activity = time.monotonic()
        self._last_activity_wall = time.time()

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
            # 空转周期也要推进延迟重扫，否则移入目录后没有新事件就永远不会补扫
            self._process_pending_rescans()
            if not changes:
                continue
            self._handle_changes(changes)
            self._mark_activity()

    def _handle_changes(self, changes: set[tuple[Change, str]]):
        """
        将 watchfiles 原始变更转换为目录监控事件。
        :param changes: watchfiles 返回的变更集合
        """
        self._dispatch_changes(self._expand_added_directories(changes))

    def _dispatch_changes(self, changes: set[tuple[Change, str]]):
        """
        将变更集合逐个派发给回调。
        :param changes: 已展开的变更集合
        """
        for change_type, path_str in sorted(changes, key=lambda item: item[1]):
            # 批量整理可能持续较久，逐个文件刷新心跳，避免被误判为静默失效
            self._mark_activity()
            if change_type not in self._HANDLE_CHANGES:
                continue
            event_path = Path(path_str)
            event = self._build_event(change_type=change_type, event_path=event_path)
            if not event:
                # 「文件已消失」与「挂载抖动瞬时不可见」在此刻无法区分，静默丢弃就是
                # 永久漏件，一律登记重试：真删除的文件由重试队列在下个周期确认后自动放弃
                self._notify_unreadable(event_path)
                continue
            if event.is_directory:
                continue
            file_size = self._get_file_size(event_path)
            if file_size is None:
                # 读取失败通常是挂载抖动，直接丢弃就是永久漏件，交给回调登记重试
                self._notify_unreadable(event_path)
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

    @property
    def DIRECTORY_RESCAN_DELAYS(self) -> tuple[int, ...]:  # noqa: N802 保持与原类常量同名，兼容既有引用/测试
        """
        获取当前生效的重扫轮次延迟秒数：每次访问都会重新解析 MONITOR_RESCAN_DELAYS
        配置，配置热更新后无需重建监控线程即可生效；解析失败或未配置时回退默认值。
        :return: 重扫轮次延迟秒数元组
        """
        return self._parse_rescan_delays(getattr(settings, "MONITOR_RESCAN_DELAYS", None))

    @classmethod
    def _parse_rescan_delays(cls, raw: Optional[str]) -> tuple[int, ...]:
        """
        解析 MONITOR_RESCAN_DELAYS 配置为重扫轮次延迟秒数元组。
        :param raw: 配置原始字符串，形如 "30,120,600,1800"
        :return: 重扫轮次延迟秒数元组，解析失败或为空时回退 DEFAULT_RESCAN_DELAYS
        """
        if not raw or not raw.strip():
            return cls.DEFAULT_RESCAN_DELAYS
        try:
            delays = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
            if not delays or any(delay <= 0 for delay in delays):
                raise ValueError(f"重扫延迟必须是正整数: {raw}")
            return delays
        except (TypeError, ValueError) as err:
            logger.warn(f"MONITOR_RESCAN_DELAYS 配置无效（{raw!r}），"
                        f"回退默认值 {cls.DEFAULT_RESCAN_DELAYS}: {err}")
            return cls.DEFAULT_RESCAN_DELAYS

    @staticmethod
    def _is_descendant_of_any(path: Path, candidates: set[Path]) -> bool:
        """
        判断 path 是否是 candidates 中某个目录的子孙路径（不含自身）。
        :param path: 待判断路径
        :param candidates: 候选祖先目录集合
        :return: 是否存在祖先命中
        """
        return any(candidate in path.parents for candidate in candidates)

    def _expand_added_directories(self, changes: set[tuple[Change, str]]) -> set[tuple[Change, str]]:
        """
        将整体移入监控范围的新增目录展开为内部文件事件。
        :param changes: watchfiles 返回的变更集合
        :return: 包含目录内新增文件的变更集合
        """
        expanded_changes = set(changes)
        # 大目录树整体移入监控范围时，changes 里每一层子目录都会各自产生一次
        # added 事件；先收集本批全部新增目录路径，用于判断某个新增目录是否还有
        # 祖先目录也在本批新增中——只有「顶层」新增目录需要登记重扫，顶层目录的
        # rglob 已经递归覆盖了全部子孙目录的内容，子目录重扫条目纯属冗余，还会在
        # 大目录树场景下迅速打满重扫队列
        added_dirs = {Path(path_str) for change_type, path_str in changes if change_type == Change.added}
        for change_type, path_str in changes:
            if change_type != Change.added:
                continue
            event_path = Path(path_str)
            try:
                if not event_path.is_dir():
                    continue
            except OSError as err:
                logger.debug(f"读取新增路径类型失败: {event_path} - {err}")
                continue
            nested_paths, _, _ = self._collect_directory_files(event_path, exclude=set())
            for nested_path_str in nested_paths:
                expanded_changes.add((Change.added, nested_path_str))
            if self._is_descendant_of_any(event_path, added_dirs):
                continue
            # 目录内容在 FUSE 上可能延迟可见，安排延迟重扫补齐本次看不到的文件
            self._schedule_rescan(event_path, seen=nested_paths)
        return expanded_changes

    def _collect_directory_files(self, directory: Path, exclude: set[str]) -> tuple[set[str], bool, bool]:
        """
        收集目录内需要处理的文件路径。
        :param directory: 目录
        :param exclude: 需要排除的路径（已处理过的）
        :return: 三元组 (文件路径集合, 目录是否已不存在（终态，不算失败）,
                 目录整体扫描是否失败（如顶层 rglob 抛出 OSError，通常是 FUSE
                 瞬时抖动，属于可重试的暂时性失败；与单个条目读取失败区分开，
                 后者不影响整体遍历，不计入失败）
        """
        collected: set[str] = set()
        try:
            if not directory.is_dir():
                # 目录已被删除或从未存在，是终态而非抖动，调用方应据此让条目
                # 直接出队，不再计入失败重试
                return collected, True, False
        except OSError as err:
            logger.debug(f"读取目录状态失败，暂视为可重试的扫描失败: {directory} - {err}")
            return collected, False, True
        try:
            for nested_path in directory.rglob("*"):
                try:
                    if not nested_path.is_file():
                        continue
                except OSError as err:
                    # 单个条目读取失败不应中断整个目录的遍历，不算整体扫描失败
                    logger.debug(f"读取目录内条目失败: {nested_path} - {err}")
                    continue
                nested_path_str = nested_path.as_posix()
                if nested_path_str in exclude:
                    continue
                if self._watch_filter(Change.added, nested_path_str):
                    collected.add(nested_path_str)
        except OSError as err:
            # 顶层 rglob 中断代表整个目录遍历失败（如 FUSE 瞬时抖动），与「目录
            # 已删除」区分开：这里应视为可重试的暂时性失败，而不是静默返回空结果
            logger.debug(f"扫描新增目录失败: {directory} - {err}")
            return collected, False, True
        return collected, False, False

    def _schedule_rescan(self, directory: Path, seen: set[str]):
        """
        安排一个新增目录的延迟重扫。
        :param directory: 新增目录
        :param seen: 首次展开时已处理的文件路径
        """
        if not self.DIRECTORY_RESCAN_DELAYS:
            return
        if any(item["path"] == directory for item in self._pending_rescans):
            # readdir 闪断等原因可能让同一目录产生两次 added 事件，从而被重复
            # 展开、重复调用到这里；重复登记只会造成队列膨胀和重复扫描——新事件
            # 覆盖到的文件已经在本次展开时派发过，保留已有条目、由它继续推进
            # 重扫轮次即可
            logger.debug(f"目录已在待重扫队列中，跳过重复登记: {directory}")
            return
        if self._is_descendant_of_any(directory, {item["path"] for item in self._pending_rescans}):
            # 祖先目录的重扫会用 rglob 递归覆盖到这里，无需为子孙目录单独登记
            logger.debug(f"目录的祖先已在待重扫队列中，跳过重复登记: {directory}")
            return
        if len(self._pending_rescans) >= self.MAX_PENDING_RESCANS:
            # 队列打满意味着可能有目录的重扫机会被挤掉，之后可能永久漏文件，
            # 需要 warn 级别可见，而不是默默丢弃
            logger.warn(f"新增目录重扫队列已满（上限 {self.MAX_PENDING_RESCANS}），跳过登记: {directory}")
            return
        self._pending_rescans.append({
            "path": directory,
            "seen": set(seen),
            "round": 0,
            "due": time.monotonic() + self.DIRECTORY_RESCAN_DELAYS[0],
            "failures": 0,
        })

    def _process_pending_rescans(self):
        """
        对到期的新增目录做延迟重扫，补回首次展开时尚不可见的文件。
        """
        if not self._pending_rescans:
            return
        now = time.monotonic()
        due_items = [item for item in self._pending_rescans if item["due"] <= now]
        if not due_items:
            return
        self._pending_rescans = [item for item in self._pending_rescans if item["due"] > now]
        for item in due_items:
            directory = item["path"]
            new_paths, is_missing, scan_failed = self._collect_directory_files(directory, exclude=item["seen"])
            if is_missing:
                # 目录已不存在，是终态：直接出队，不再重新入队，也不计入失败重试
                logger.debug(f"待重扫目录已不存在，结束重扫: {directory}")
                continue
            if scan_failed:
                # 整体扫描失败（通常是 FUSE 瞬时抖动）：本轮不消耗重扫轮次，
                # 让条目原样重新入队，下一个监控周期立即再试；但要有失败上限，
                # 避免目录长期不可访问时无限重试、占满队列
                item["failures"] = item.get("failures", 0) + 1
                if item["failures"] >= self.MAX_RESCAN_FAILURES:
                    logger.warn(
                        f"目录延迟重扫连续失败 {item['failures']} 次，放弃重扫: {directory}")
                    continue
                logger.debug(
                    f"目录延迟重扫本轮扫描失败（第 {item['failures']} 次），不消耗轮次，稍后重试: {directory}")
                self._pending_rescans.append(item)
                continue
            # 本轮扫描成功，重置失败计数
            item["failures"] = 0
            if new_paths:
                logger.info(f"新增目录延迟重扫发现 {len(new_paths)} 个此前不可见的文件: {directory}")
                self._dispatch_changes({(Change.added, path_str) for path_str in new_paths})
                item["seen"].update(new_paths)
            next_round = item["round"] + 1
            if next_round < len(self.DIRECTORY_RESCAN_DELAYS):
                item["round"] = next_round
                item["due"] = now + self.DIRECTORY_RESCAN_DELAYS[next_round]
                self._pending_rescans.append(item)

    def _notify_unreadable(self, event_path: Path):
        """
        通知回调登记读取失败的事件，等待重试。
        :param event_path: 事件文件路径
        """
        handler = getattr(self._callback, "event_unreadable", None)
        if not callable(handler):
            return
        try:
            handler(event_path=event_path)
        except Exception as err:
            logger.error(f"登记待重试监控事件失败: {event_path} - {err}")

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
