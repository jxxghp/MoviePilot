import time
import threading
import traceback
from functools import partial
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler

from app.application.directory import DirectoryHelper
from app.application.messaging.message import MessageHelper
from app.runtime.log import logger
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.poller import RemotePoller
from app.monitor.recovery import RecoveryExecutor, RecoveryState, probe_path
from app.monitor.snapshot import SnapshotStore
from app.monitor.syslimits import decide_monitor_mode, get_system_optimization_tips
from app.monitor.watcher import LocalDirectoryWatcher
from app.schemas.types import SystemConfigKey
from app.runtime.reload import ConfigReloadMixin
from app.foundation.singleton import SingletonClass
from app.adapters.system.host import SystemUtils
from app.runtime.settings import get_runtime_setting


class Monitor(ConfigReloadMixin, metaclass=SingletonClass):
    """
    目录监控门面，单例模式：装配本地/远程监控、维护生命周期与健康检查。
    """
    # watcher/scheduler 在构造期启动；异常时保留实例供启动失败屏障收口。
    _retain_failed_singleton = True
    # 除目录配置外，同时监听仅在监控线程创建时读取的环境变量：这两项经
    # /system/env 保存后运行时值虽已更新，但已运行的监控不会重新决策模式，
    # 必须触发 init() 全量重建才能生效（MONITOR_RESCAN_DELAYS 为实时解析，无需在列）
    CONFIG_WATCH = {SystemConfigKey.Directories.value,
                    "MONITOR_NETWORK_FAST_MODE",
                    "MONITOR_POLL_DELAY_NETWORK"}
    # 目录监控健康检查间隔（秒）
    WATCHDOG_INTERVAL = 60
    # 连续多少个健康检查周期无新增重启后才宣告恢复，避免反复崩溃时告警刷屏
    RECOVERY_STABLE_CYCLES = 5
    # 补偿扫描的时间回溯余量（秒），覆盖心跳与文件落地之间的时间差
    COMPENSATION_MARGIN = 60
    # 监控内部退避重启的停摆窗口无法从外部精确观测（重启在 watcher 线程内部完成，
    # 健康检查只能看到累计重启次数），保守取「健康检查周期 + 最长退避 + 余量」
    RESTART_STALL_LOOKBACK = WATCHDOG_INTERVAL + max(LocalDirectoryWatcher.RESTART_BACKOFF) + COMPENSATION_MARGIN
    # 单次补偿扫描最多送入整理链的文件数，避免超大目录把整理链和数据库压垮
    MAX_COMPENSATION_FILES = 2000
    # 恢复动作（重建监控、重试队列、挂载探测）单个健康检查周期的最长等待秒数。
    # 这些动作全部会触碰挂载，一律在一次性工作线程里执行，看门狗只等待有限时间；
    # 取健康检查周期的一半，保证看门狗自身永远不会被拖过下一个周期
    RECOVERY_TIMEOUT = WATCHDOG_INTERVAL // 2
    # 隔离目录的挂载探测超时秒数（子进程，超时可被 kill）
    MOUNT_PROBE_TIMEOUT = 10
    # 恢复动作的 key 前缀/常量，用于按目录隔离在途动作
    REBUILD_KEY_PREFIX = "rebuild:"
    PROBE_KEY = "probe"
    PENDING_KEY = "pending"
    RELOAD_STOP_TIMEOUT = 30.0
    LIFECYCLE_CLOSE_TIMEOUT = 90.0

    def __init__(self) -> None:
        """初始化目录监控依赖、owner 注册表与当前 lifespan 状态。"""
        super().__init__()
        # 生命周期操作串行化；永久关闭请求用 Event 提前封口，不能被阻塞 I/O 挡住。
        self._lifecycle_lock = threading.RLock()
        self._owner_lock = threading.Lock()
        self._work_stop_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._closed = False
        self._compensation_threads: Dict[int, threading.Thread] = {}
        self._scheduler_shutdown_thread: Optional[threading.Thread] = None
        self._scheduler_shutdown_succeeded = False
        # 本地目录监控服务
        self._watchers = []
        # 已请求停止但尚未退出的 watcher，只保留 owner 身份，不再参与健康检查
        self._retired_watchers = []
        # 本地目录监控列表读写锁
        self._watcher_lock = Lock()
        # 启动失败待重试的本地监控配置
        self._pending_locals: List[Dict[str, Any]] = []
        # 已告警的监控目录及其告警阶段，避免重复推送，同时保证故障升级能再推一次
        self._alerted_paths: Dict[str, str] = {}
        # 各监控目录已告警过的自动重启次数
        self._restart_marks: Dict[str, int] = {}
        # 各监控目录连续稳定的健康检查周期数
        self._stable_cycles: Dict[str, int] = {}
        # 判定为挂载级故障、已暂停一切访问的监控目录（path -> 隔离状态）
        self._isolated: Dict[str, Dict[str, Any]] = {}
        # 探测通过、等待下一周期重建的目录。不能靠 is_stalled 重新发现：
        # 进入隔离前 __rebuild_watcher 已调用过 watcher.stop()，停止标志置位后
        # is_stalled() 恒为 False，检测环节再也认不出它需要重建
        self._pending_rebuild: Dict[str, Any] = {}
        # 触碰挂载的恢复动作执行器，把 block 型故障挡在看门狗线程之外
        self._recovery = RecoveryExecutor()
        # 定时服务
        self._scheduler = None
        # 整理分发器
        self._dispatcher = TransferDispatcher()
        # 快照存储
        self._store = SnapshotStore()
        # 远程轮询监控
        self._poller = RemotePoller(store=self._store, dispatcher=self._dispatcher,
                                    alert_cb=self.__poller_alert)
        # 启动目录监控和文件整理
        if not self.init():
            raise RuntimeError("目录监控 owner 初始化未收敛")

    def on_config_changed(self) -> None:
        """配置变化时重建监控；lifespan 封口后拒绝重新启动。"""
        if self._shutdown_event.is_set():
            logger.info("目录监控已进入生命周期封口，忽略配置热更新")
            return
        self.init()

    def get_reload_name(self):
        return "目录监控"

    def save_snapshot(self, storage: str, snapshot: Dict, file_count: int = 0,
                      last_snapshot_time: Optional[float] = None):
        """
        保存快照到文件缓存。
        """
        self._store.save(storage, snapshot, file_count=file_count, last_snapshot_time=last_snapshot_time)

    def load_snapshot(self, storage: str) -> Optional[Dict]:
        """
        从文件缓存加载快照。
        """
        return self._store.load(storage)

    def reset_snapshot(self, storage: str) -> bool:
        """
        重置快照，强制下次扫描时重新建立基准。
        """
        return self._store.reset(storage)

    def force_full_scan(self, storage: str, mon_path: Path) -> bool:
        """
        强制全量扫描并处理所有文件（包括已存在的文件）。
        """
        if not self.__accepting_work():
            return False
        return self._poller.force_full_scan(storage=storage, mon_path=mon_path)

    @staticmethod
    def adjust_monitor_interval(file_count: int) -> int:
        """
        根据文件数量动态调整监控间隔。
        """
        return SnapshotStore.adjust_interval(file_count)

    @staticmethod
    def compare_snapshots(old_snapshot: Dict, new_snapshot: Dict) -> Dict[str, List]:
        """
        比对快照，找出变化的文件。
        """
        return SnapshotStore.compare(old_snapshot, new_snapshot)

    def init(self, timeout: float = RELOAD_STOP_TIMEOUT) -> bool:
        """在旧 owner 收敛后启动一代监控，关闭中的生命周期拒绝重开。"""
        if self._shutdown_event.is_set():
            logger.info("目录监控生命周期已关闭，跳过启动")
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._lifecycle_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
        ):
            logger.error("目录监控未在 %.1f 秒内取得重载所有权", max(0.0, timeout))
            return False
        try:
            if self._shutdown_event.is_set():
                return False
            if not self.__stop_owned(deadline=deadline, close=False):
                logger.error("旧目录监控 owner 未收敛，取消本次配置热更新")
                return False
            if self._shutdown_event.is_set():
                return False
            if not self._recovery.reopen():
                logger.error("目录监控恢复线程仍未退出，取消本次配置热更新")
                return False
            if self._shutdown_event.is_set():
                self._recovery.request_stop()
                return False
            self._work_stop_event.clear()
            return self.__initialize_monitors()
        finally:
            self._lifecycle_lock.release()

    def __initialize_monitors(self) -> bool:
        """
        在已取得生命周期所有权后启动监控。

        永久关闭请求可以在本方法阻塞于 FUSE 时从其他线程提前设置；每个可能产生
        新 owner 的边界都重新检查，确保解冻后只能退出，不能穿透旧生命周期。
        """
        if not self.__accepting_work():
            return False

        # 读取目录配置
        monitor_dirs = DirectoryHelper().get_download_dirs()
        if not self.__accepting_work():
            return False
        if not monitor_dirs:
            logger.info("未找到任何目录监控配置")
            return True

        messagehelper = MessageHelper()

        # 先筛出有效的监控配置，再按下载目录去重，避免非监控配置顶掉监控配置
        valid_dirs = []
        for mon_dir in monitor_dirs:
            if not self.__accepting_work():
                return False
            if not mon_dir.library_path:
                logger.warn(f"跳过监控配置 {mon_dir.download_path}：未设置媒体库目录")
                continue
            if mon_dir.monitor_type != "monitor":
                logger.debug(f"跳过监控配置 {mon_dir.download_path}：监控类型为 {mon_dir.monitor_type}")
                continue
            valid_dirs.append(mon_dir)

        deduped: Dict[str, Any] = {}
        for mon_dir in valid_dirs:
            key = f"{mon_dir.storage}_{mon_dir.download_path}"
            if key in deduped:
                logger.warn(f"监控配置重复，忽略后一条: {mon_dir.download_path}"
                            f"（媒体库 {mon_dir.library_path}）")
                continue
            deduped[key] = mon_dir
        monitor_dirs = list(deduped.values())
        logger.info(f"找到 {len(monitor_dirs)} 个目录监控配置")

        # 启动定时服务进程
        if not self.__accepting_work():
            return False
        self._scheduler = BackgroundScheduler(timezone=get_runtime_setting("TZ"))

        mon_storages: Dict[str, List[Path]] = {}
        # 本地监控启动结果计数，用于输出真实的启动总结
        local_started = 0
        local_failed = 0
        for mon_dir in monitor_dirs:
            if not self.__accepting_work():
                self.__abort_interrupted_startup()
                return False
            # 检查媒体库目录是不是下载目录的子目录
            mon_path = Path(mon_dir.download_path)
            target_path = Path(mon_dir.library_path)
            if target_path.is_relative_to(mon_path):
                logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控！")
                messagehelper.put(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控", title="目录监控")
                continue

            # 启动监控
            if mon_dir.storage == "local":
                if self.__start_local_monitor(mon_path=mon_path, monitor_mode=mon_dir.monitor_mode):
                    local_started += 1
                else:
                    local_failed += 1
            else:
                mon_storages.setdefault(mon_dir.storage, []).append(mon_path)

        for storage, paths in mon_storages.items():
            if not self.__accepting_work():
                self.__abort_interrupted_startup()
                return False
            # 远程目录监控 - 使用智能间隔
            # 先尝试加载已有快照获取文件数量
            snapshot_data = self._store.load(storage)
            snapshot_is_current = (
                snapshot_data
                and snapshot_data.get('version') == SnapshotStore.VERSION
            )
            file_count = snapshot_data.get('file_count', 0) if snapshot_is_current else 0
            interval = SnapshotStore.adjust_interval(file_count)
            for path in paths:
                logger.info(f"正在启动远程目录监控: {path} [{storage}]")
            logger.info("*** 重要提示：远程目录监控只处理新增和修改的文件，不会处理监控启动前已存在的文件 ***")
            if snapshot_data and not snapshot_is_current:
                logger.info(f"检测到旧版远程快照，将在首次轮询后重新校准: {storage}")
            logger.info(f"上次快照文件数量: {file_count}, 监控间隔: {interval}分钟")

            self._scheduler.add_job(
                self.polling_observer,
                'interval',
                minutes=interval,
                kwargs={
                    'storage': storage,
                    'mon_paths': paths
                },
                id=f"monitor_{storage}",
                replace_existing=True
            )
            logger.info(f"✓ 远程目录监控已启动: [间隔: {interval}分钟]")

        # 监控健康检查：重建异常监控线程、重试启动失败目录、重试历史查询失败的文件
        if local_started or local_failed or mon_storages:
            self._scheduler.add_job(
                self.watchdog,
                'interval',
                seconds=self.WATCHDOG_INTERVAL,
                id="monitor_watchdog",
                replace_existing=True
            )
            logger.info(f"✓ 目录监控健康检查已启动: [间隔: {self.WATCHDOG_INTERVAL}秒]")

        # 启动定时服务
        if not self.__accepting_work():
            self.__abort_interrupted_startup()
            return False
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()
            logger.info("定时监控服务已启动")

        # 输出监控总结，报告实际启动成功数而不是配置数
        remote_count = sum(len(paths) for paths in mon_storages.values())
        summary = f"目录监控启动完成: 本地监控 {local_started} 个成功"
        if local_failed:
            summary += f"、{local_failed} 个失败（将自动退避重试）"
        summary += f"，远程监控 {remote_count} 个"
        if local_failed:
            logger.warn(summary)
        else:
            logger.info(summary)
        return True

    def __accepting_work(self) -> bool:
        """判断当前 monitor 代是否仍允许创建 owner 和派发整理工作。"""
        return not self._work_stop_event.is_set() and not self._shutdown_event.is_set()

    @property
    def lifecycle_closed(self) -> bool:
        """返回当前 Monitor 是否已经被应用生命周期永久封口。"""
        return self._closed or self._shutdown_event.is_set()

    def reopen(self, timeout: float = RELOAD_STOP_TIMEOUT) -> bool:
        """确认旧 owner 已收敛后，为新的应用 lifespan 显式解除永久封口。"""
        if not self.lifecycle_closed:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._lifecycle_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
        ):
            logger.error("旧目录监控生命周期仍在收尾，无法重新开启")
            return False
        try:
            if not self.__stop_owned(deadline=deadline, close=True):
                logger.error("旧目录监控 owner 仍未收敛，无法重新开启")
                return False
            if not self._recovery.reopen():
                logger.error("旧目录监控恢复线程仍存活，无法重新开启")
                return False
            self._closed = False
            self._shutdown_event.clear()
            return True
        finally:
            self._lifecycle_lock.release()

    def __abort_interrupted_startup(self) -> None:
        """关闭请求穿透阻塞启动后，在当前生命周期锁内收拢已经创建的 owner。"""
        logger.info("目录监控启动期间收到关闭请求，正在回收本次已创建 owner")
        self.__stop_owned(
            deadline=time.monotonic() + self.RELOAD_STOP_TIMEOUT,
            close=self._shutdown_event.is_set(),
        )

    def __start_local_monitor(self, mon_path: Path, monitor_mode: str) -> bool:
        """
        启动单个本地目录监控，失败时登记待重试。
        :param mon_path: 监控目录
        :param monitor_mode: 配置的监控模式
        :return: 是否启动成功
        """
        if not self.__accepting_work():
            return False
        logger.info(f"正在启动本地目录监控: {mon_path}")
        logger.info("*** 重要提示：目录监控只处理新增和修改的文件，不会处理监控启动前已存在的文件 ***")

        try:
            # 检查是否需要使用轮询模式（兼容模式/网络存储不做启动期目录遍历）
            use_polling, reason, limits, file_count = decide_monitor_mode(mon_path, monitor_mode)
            logger.info(f"监控模式决策: {reason}")

            mode_name = "兼容模式(轮询)" if use_polling else "快速模式"
            logger.info(f"使用{mode_name}监控 {mon_path}")
            if file_count is not None:
                logger.info(f"监控目录 {mon_path} 包含约 {file_count} 个文件")
            if not use_polling and limits:
                if limits['warnings']:
                    for warning in limits['warnings']:
                        logger.warn(f"系统限制警告: {warning}")
                if limits['max_user_watches'] > 0 and file_count is not None:
                    usage_percent = (file_count / limits['max_user_watches']) * 100
                    logger.info(
                        f"系统监控资源使用率: {usage_percent:.1f}% ({file_count}/{limits['max_user_watches']})")

            # 网络/FUSE 挂载轮询降频，减少监控自身对挂载后端的持续 stat 压力
            poll_delay_ms = None
            if use_polling and SystemUtils.is_network_filesystem(mon_path):
                poll_delay_ms = (get_runtime_setting("MONITOR_POLL_DELAY_NETWORK")
                                 or LocalDirectoryWatcher.POLL_DELAY_NETWORK_MS)
                logger.info(f"检测到网络文件系统，轮询扫描间隔调整为 {poll_delay_ms}ms: {mon_path}")

            watcher = LocalDirectoryWatcher(
                mon_path=mon_path,
                callback=self,
                force_polling=True if use_polling else None,
                poll_delay_ms=poll_delay_ms
            )
            # 启动成功后再登记，避免失败的监控残留在列表中
            watcher.start()
            with self._watcher_lock:
                if not self.__accepting_work():
                    watcher.stop()
                    if watcher.is_alive():
                        self._retired_watchers.append(watcher)
                    return False
                self._watchers.append(watcher)
                self._pending_locals = [
                    pending for pending in self._pending_locals
                    if pending["mon_path"] != mon_path
                ]
            self.__clear_alert(mon_path, f"本地目录监控已恢复: {mon_path} [{mode_name}]")

            logger.info(f"✓ 本地目录监控已启动: {mon_path} [{mode_name}]")
            return True

        except Exception as e:
            self.__handle_start_failure(mon_path=mon_path, monitor_mode=monitor_mode, err=e)
            return False

    def __handle_start_failure(self, mon_path: Path, monitor_mode: str, err: Exception) -> None:
        """
        处理本地目录监控启动失败，登记待重试并按需告警。
        :param mon_path: 监控目录
        :param monitor_mode: 配置的监控模式
        :param err: 启动异常
        """
        if not self.__accepting_work():
            return
        err_msg = str(err)
        logger.error(f"启动本地目录监控失败: {mon_path}")
        logger.error(f"错误详情: {err_msg}")

        if "inotify" in err_msg.lower():
            logger.error("inotify 相关错误，这通常是由于系统监控数量限制导致的")
            logger.error("解决方案:")
            for tip in get_system_optimization_tips():
                logger.error(f"  {tip}")
            logger.error("执行上述命令后重启 MoviePilot")
        elif "permission" in err_msg.lower():
            logger.error("权限错误，请检查 MoviePilot 是否有足够的权限访问监控目录")
        elif isinstance(err, (FileNotFoundError, NotADirectoryError)):
            logger.error("监控目录当前不可用，网络存储/FUSE 挂载可能尚未就绪，将自动重试")
        elif monitor_mode != "compatibility":
            logger.error("建议尝试使用兼容模式进行监控")

        with self._watcher_lock:
            if all(pending["mon_path"] != mon_path for pending in self._pending_locals):
                self._pending_locals.append({
                    "mon_path": mon_path,
                    "monitor_mode": monitor_mode
                })
        self.__send_alert(mon_path,
                          f"启动本地目录监控失败: {mon_path}\n错误: {err_msg}\n"
                          f"将自动退避重试")

    def watchdog(self) -> None:
        """
        目录监控健康检查：检测监控线程状态并驱动恢复。

        本方法是全局自愈的单点，因此**只做纯内存的检测与判定**：任何会触碰挂载
        的动作（重建监控、重试启动、重试整理、挂载探测）都交给一次性工作线程，
        看门狗只等待有限时间。FUSE 挂载进入「请求永不返回」状态时，内联执行这些
        动作会把看门狗冻死在它自己要修复的挂载上，随后停滞检测、告警、重试驱动
        全部静默失效——这正是全进程雪崩的起点。
        """
        if not self.__accepting_work():
            return
        try:
            broken = self.__check_watchers()
            if not self.__accepting_work():
                return
            self.__drive_recovery(broken)
        except Exception as e:
            logger.error(f"目录监控健康检查出现错误：{e}\n{traceback.format_exc()}")

    def __check_watchers(self) -> List[LocalDirectoryWatcher]:
        """
        检查本地目录监控线程状态，返回需要重建的监控。

        全程只读内存状态（线程存活标志、心跳时间戳、重启计数），不做任何文件
        系统访问，确保挂载无响应时检测环节本身永远不会被阻塞。
        :return: 需要重建的监控列表
        """
        with self._watcher_lock:
            watchers = list(self._watchers)
            self._retired_watchers = [
                watcher for watcher in self._retired_watchers if watcher.is_alive()
            ]
            isolated = set(self._isolated)
            # 探测已确认挂载恢复的目录，本轮直接送去重建
            resumed = list(self._pending_rebuild.values())
            self._pending_rebuild.clear()
        broken: List[LocalDirectoryWatcher] = list(resumed)
        for watcher in watchers:
            key = str(watcher.watch_path)
            if key in isolated:
                # 已判定挂载级故障：重建只会再冻死一个线程，等探测确认挂载
                # 恢复应答后由 __probe_isolated 统一重建
                continue
            if watcher.is_stalled():
                reason = f"监控循环超过 {LocalDirectoryWatcher.STALL_TIMEOUT} 秒无任何活动，判定为静默失效"
            elif not watcher.is_alive():
                reason = "监控线程已退出"
            else:
                # 线程已自愈，但崩溃过就要告警，避免自动重启把故障变成新的静默
                if watcher.restart_count > self._restart_marks.get(key, 0):
                    self._restart_marks[key] = watcher.restart_count
                    self._stable_cycles[key] = 0
                    self.__send_alert(watcher.watch_path,
                                      f"目录监控发生错误并已自动重启"
                                      f"（累计 {watcher.restart_count} 次）: {watcher.watch_path}")
                    # 内部退避重启同样是停摆：轮询模式下重启会重建基线快照，停摆窗口内
                    # 落地的文件会被新基线静默吸收，永远不再产生事件，必须补扫。
                    # 重启计数只在增长时进入本分支，同一次重启不会被反复补扫
                    self.__start_compensation(mon_path=watcher.watch_path,
                                              since=time.time() - self.RESTART_STALL_LOOKBACK)
                else:
                    # 稳定满恢复窗口才宣告恢复，避免反复崩溃时告警/恢复消息来回刷屏
                    self._stable_cycles[key] = self._stable_cycles.get(key, 0) + 1
                    if self._stable_cycles[key] >= self.RECOVERY_STABLE_CYCLES:
                        self.__clear_alert(watcher.watch_path, f"目录监控已恢复正常: {watcher.watch_path}")
                continue
            logger.error(f"目录监控异常: {watcher.watch_path} - {reason}，正在重建监控线程 ...")
            self.__send_alert(watcher.watch_path,
                              f"目录监控异常: {watcher.watch_path}\n原因: {reason}\n正在自动重建监控")
            broken.append(watcher)
        return broken

    def __drive_recovery(self, broken: List[LocalDirectoryWatcher]) -> None:
        """
        把所有会触碰挂载的恢复动作派发到一次性工作线程，并等待有限时间。

        重建动作超时（或上一轮的重建仍冻着）即判定为挂载级故障：这已经不是单个
        目录的问题，继续每 60 秒重试只会不断泄漏冻死的线程，必须转入隔离，改由
        可放弃的子进程探测来确认挂载何时恢复。
        :param broken: 需要重建的监控列表
        """
        if not self.__accepting_work():
            return
        actions: Dict[str, Callable[[], None]] = {}
        rebuilds: Dict[str, LocalDirectoryWatcher] = {}
        for watcher in broken:
            key = f"{self.REBUILD_KEY_PREFIX}{watcher.watch_path}"
            rebuilds[key] = watcher
            actions[key] = partial(self.__rebuild_watcher, watcher)
        if self._isolated:
            actions[self.PROBE_KEY] = self.__probe_isolated
        actions[self.PENDING_KEY] = self.__drive_pending

        results = self._recovery.run(actions, timeout=self.RECOVERY_TIMEOUT)
        if not self.__accepting_work():
            return

        for key, state in results.items():
            if state is RecoveryState.COMPLETED:
                continue
            watcher = rebuilds.get(key)
            if watcher is not None:
                self.__enter_isolation(watcher)
            else:
                # 探测与重试驱动超时不升级为隔离：它们跨多个目录，无法归因到
                # 具体挂载，下个周期由执行器的 BUSY 判定自动跳过，不会泄漏线程
                logger.warn(f"目录监控恢复动作未在 {self.RECOVERY_TIMEOUT} 秒内完成"
                            f"（{state.value}），将在后续健康检查周期重试: {key}")

    def __enter_isolation(self, watcher: LocalDirectoryWatcher) -> None:
        """
        将一个监控目录转入挂载级故障隔离：停止对它的一切新访问，等待探测恢复。
        :param watcher: 重建未能返回的监控
        """
        if not self.__accepting_work():
            return
        key = str(watcher.watch_path)
        with self._watcher_lock:
            if key in self._isolated:
                return
            self._isolated[key] = {
                "watcher": watcher,
                "since": time.time(),
                "failures": 0,
            }
        logger.error(f"目录监控重建在挂载上无响应，判定为挂载级故障，"
                     f"已暂停对该目录的所有访问并转入周期探测: {watcher.watch_path}")
        self.__send_alert(watcher.watch_path,
                          f"目录监控挂载无响应: {watcher.watch_path}\n"
                          f"已暂停对该目录的所有访问，正在周期探测挂载，恢复后将自动重建监控并补扫",
                          stage="isolated")

    def __probe_isolated(self) -> None:
        """
        对隔离中的监控目录做可放弃探测，挂载恢复应答后解除隔离并重建监控。

        运行在恢复工作线程里：探测本身由子进程执行（见 recovery.probe_path），
        超时可被 kill，因此本线程不会像内联 stat 那样永久冻死。探测通过后的重建
        仍有极小概率再次卡住，届时本线程会被下一轮的 BUSY 判定跳过，不再泄漏。
        """
        if not self.__accepting_work():
            return
        with self._watcher_lock:
            keys = list(self._isolated)
        for key in keys:
            if not self.__accepting_work():
                return
            mon_path = Path(key)
            if not probe_path(mon_path, timeout=self.MOUNT_PROBE_TIMEOUT):
                if not self.__accepting_work():
                    return
                with self._watcher_lock:
                    entry = self._isolated.get(key)
                    if not entry:
                        continue
                    entry["failures"] += 1
                    failures = entry["failures"]
                    since = entry["since"]
                logger.warn(f"挂载探测未通过（累计 {failures} 次，已隔离 "
                            f"{int(time.time() - since)} 秒），继续隔离: {mon_path}")
                continue
            if not self.__accepting_work():
                return
            with self._watcher_lock:
                entry = self._isolated.pop(key, None)
            if not entry:
                continue
            logger.info(f"✓ 挂载探测通过，解除隔离并登记重建: {mon_path}")
            self.__clear_alert(mon_path, f"目录监控挂载已恢复响应，正在重建监控: {mon_path}")
            # 只登记、不在此处重建。重建会触碰挂载，若挂载能应答探测却在重建时再次
            # 挂死，这条探测线程将永不返回，全局 PROBE_KEY 从此恒为 BUSY——列表中
            # 其余隔离目录连探测机会都没有了，「按目录隔离」的承诺就此失效。
            # 交由下一周期的 __check_watchers 走 rebuild:<path> 路径，天然按目录隔离。
            with self._watcher_lock:
                self._pending_rebuild[key] = entry["watcher"]

    def __drive_pending(self) -> None:
        """
        驱动两条待重试队列。两者都会访问挂载（启动重试走目录遍历与 exists，
        整理重试走 stat），必须在恢复工作线程里执行而不是看门狗线程里。
        """
        if not self.__accepting_work():
            return
        self.__retry_pending_locals()
        if self.__accepting_work():
            self._dispatcher.retry_pending()

    def __rebuild_watcher(self, watcher: LocalDirectoryWatcher) -> None:
        """
        重建一个本地目录监控线程。
        :param watcher: 需要重建的监控
        """
        if not self.__accepting_work():
            return
        # 卡死的线程阻塞在底层调用中无法强制回收，只能请求停止后由守护线程自然退出
        watcher.stop()
        new_watcher = LocalDirectoryWatcher(
            mon_path=watcher.watch_path,
            callback=self,
            force_polling=watcher.force_polling,
            poll_delay_ms=watcher.poll_delay_ms
        )
        try:
            new_watcher.start()
        except Exception as e:
            logger.error(f"重建目录监控失败: {watcher.watch_path} - {e}")
            if not self.__accepting_work():
                return
            with self._watcher_lock:
                # 旧 watcher 可能仍卡在 FUSE 调用里；只有真实退出后才能移除句柄。
                self._watchers = [item for item in self._watchers if item is not watcher]
                if watcher.is_alive():
                    self._retired_watchers.append(watcher)
                if all(pending["mon_path"] != watcher.watch_path for pending in self._pending_locals):
                    self._pending_locals.append({
                        "mon_path": watcher.watch_path,
                        # 重建沿用原监控模式，force_polling 为 True 即兼容模式
                        "monitor_mode": "compatibility" if watcher.force_polling else "fast"
                    })
            return
        with self._watcher_lock:
            if not self.__accepting_work():
                new_watcher.stop()
                if new_watcher.is_alive():
                    self._retired_watchers.append(new_watcher)
                return
            registered = any(item is watcher for item in self._watchers)
            if registered:
                self._watchers = [new_watcher if item is watcher else item for item in self._watchers]
                if watcher.is_alive():
                    # stop() 只能发信号，FUSE 上的旧线程可能永久不返回；继续持有它，
                    # 让生命周期关闭如实失败，而不是把 daemon 泄漏伪装成已收敛。
                    self._retired_watchers.append(watcher)
        if not registered:
            # 卡死的重建线程可能在挂载恢复后才解冻并走到这里，而该目录此时已由
            # 隔离恢复路径重建过。此处若直接放行，新建的监控既不在 _watchers 里
            # （健康检查永远看不到它）、也没人调用 stop()，会变成与旧监控重复
            # 派发事件的孤儿线程，必须就地停掉
            logger.warn(f"目录监控已由其他路径重建，停止本次重建的冗余监控: {watcher.watch_path}")
            new_watcher.stop()
            if new_watcher.is_alive():
                with self._watcher_lock:
                    self._retired_watchers.append(new_watcher)
            return
        # 新监控的重启计数从零开始，同步重置告警基准
        self._restart_marks.pop(str(watcher.watch_path), None)
        self._stable_cycles.pop(str(watcher.watch_path), None)
        logger.info(f"✓ 目录监控已重建: {watcher.watch_path}")
        self.__clear_alert(watcher.watch_path, f"目录监控已自动恢复: {watcher.watch_path}")
        # 重建只恢复未来的事件，停摆期间落地的文件不会再产生任何事件，必须补扫
        self.__start_compensation(mon_path=watcher.watch_path,
                                  since=watcher.last_activity_time)

    def __start_compensation(self, mon_path: Path, since: float) -> None:
        """
        在后台线程发起补偿扫描，避免遍历目录阻塞健康检查周期。
        :param mon_path: 监控目录
        :param since: 停摆起点（墙钟时间戳）
        """
        if not since:
            # 从未活动过说明没有可靠的停摆起点，全量补扫代价不可控，跳过
            logger.debug(f"监控无活动记录，跳过补偿扫描: {mon_path}")
            return
        thread = Thread(
            target=self.__run_compensation,
            kwargs={"mon_path": mon_path, "since": since},
            name=f"MoviePilot-MonitorCompensation-{mon_path.name}",
            daemon=True
        )
        with self._owner_lock:
            # 与 close() 的 owner 快照共用同一把锁；封口先发生时绝不再启动，
            # 登记先发生时 close() 必然能看到并等待这条线程。
            if not self.__accepting_work():
                return
            self._compensation_threads[id(thread)] = thread
            try:
                thread.start()
            except Exception:
                self._compensation_threads.pop(id(thread), None)
                raise

    def __run_compensation(self, mon_path: Path, since: float) -> None:
        """执行补偿扫描，并在真实终态后释放 owner 句柄。"""
        current_thread = threading.current_thread()
        try:
            self.__compensate_scan(mon_path=mon_path, since=since)
        finally:
            with self._owner_lock:
                self._compensation_threads.pop(id(current_thread), None)

    def __compensate_scan(self, mon_path: Path, since: float) -> None:
        """
        补扫监控停摆期间落地的文件。

        不能用 mtime 判定「停摆期间落地」：CloudDrive2/115 等网盘挂载在转存、移动
        文件时保留原始 mtime（可能是几年前），按 mtime 过滤会让补偿扫描完全空转。
        因此把目录内所有候选文件都送入整理链，由分发器的 TTL 去重与整理历史查重挡掉
        已处理过的文件；代价是每个候选文件一次历史查询，故按 mtime 从新到旧排序并用
        MAX_COMPENSATION_FILES 限制单次规模，让名额优先给最可能是新落地的文件。
        :param mon_path: 监控目录
        :param since: 停摆起点（墙钟时间戳），仅用于统计与日志
        """
        if not self.__accepting_work():
            return
        candidates = self.__collect_compensation_files(mon_path)
        if not self.__accepting_work():
            return
        if candidates is None:
            return
        # mtime 不再作为过滤条件，但仍是「最可能是新文件」的排序依据
        candidates.sort(key=lambda item: item[1], reverse=True)
        if len(candidates) > self.MAX_COMPENSATION_FILES:
            logger.warn(f"补偿扫描候选文件 {len(candidates)} 个，超过单次上限 "
                        f"{self.MAX_COMPENSATION_FILES}，本次只处理最新的一批: {mon_path}")
            candidates = candidates[:self.MAX_COMPENSATION_FILES]
        threshold = since - self.COMPENSATION_MARGIN
        changed_count = sum(1 for candidate in candidates if candidate[1] >= threshold)
        handled = 0
        for file_path, file_modify_time, file_size in candidates:
            if not self.__accepting_work():
                return
            if self._dispatcher.handle_file(
                    storage="local",
                    event_path=file_path,
                    file_size=file_size,
                    file_modify_time=file_modify_time,
            ):
                handled += 1
        logger.info(f"✓ 目录监控补偿扫描完成，{len(candidates)} 个候选文件"
                    f"（其中 {changed_count} 个修改时间落在停摆期间）中有 {handled} 个进入整理链: {mon_path}")

    def __collect_compensation_files(self, mon_path: Path) -> Optional[List[Tuple[Path, float, int]]]:
        """
        收集补偿扫描的候选文件。
        :param mon_path: 监控目录
        :return: (文件路径, 修改时间, 文件大小) 列表，目录遍历失败时返回 None
        """
        candidates: List[Tuple[Path, float, int]] = []
        try:
            for file_path in mon_path.rglob("*"):
                # 扩展名判断是纯字符串运算，先过滤能省掉大量 FUSE 挂载上昂贵的 stat
                if not self._dispatcher.is_transfer_candidate_path(file_path):
                    continue
                try:
                    if not file_path.is_file():
                        continue
                    file_stat = file_path.stat()
                except OSError as err:
                    logger.debug(f"补偿扫描读取文件失败: {file_path} - {err}")
                    continue
                candidates.append((file_path, file_stat.st_mtime, file_stat.st_size))
        except OSError as err:
            logger.error(f"补偿扫描失败: {mon_path} - {err}")
            return None
        return candidates

    def __retry_pending_locals(self) -> None:
        """
        重试启动失败的本地目录监控，给网络存储/FUSE 挂载留出就绪时间。
        """
        if not self.__accepting_work():
            return
        with self._watcher_lock:
            pending = list(self._pending_locals)
            isolated = set(self._isolated)
        for item in pending:
            if not self.__accepting_work():
                return
            if str(item["mon_path"]) in isolated:
                # 隔离中的挂载不接受任何新访问：启动重试要走目录遍历与 exists，
                # 在「请求永不返回」的挂载上会再冻死一个线程
                continue
            # 失败次数越多重试间隔越长（按健康检查周期数退避），长时间故障时不刷屏
            if item.get("skip_cycles", 0) > 0:
                item["skip_cycles"] -= 1
                continue
            logger.info(f"重试启动本地目录监控: {item['mon_path']}")
            if not self.__start_local_monitor(mon_path=item["mon_path"], monitor_mode=item["monitor_mode"]):
                item["attempts"] = item.get("attempts", 0) + 1
                item["skip_cycles"] = min(item["attempts"], 10)

    def __send_alert(self, mon_path: Path, message: str, stage: str = "fault") -> None:
        """
        推送目录监控异常告警，同一目录在同一阶段仅推送一次。
        :param mon_path: 监控目录
        :param message: 告警内容
        :param stage: 告警阶段。故障升级为挂载级隔离是用户必须知道的状态变化
                      （监控已暂停访问、等待挂载恢复），只按目录去重会把这条
                      关键消息吞掉，因此阶段变化时重新推送
        """
        if not self.__accepting_work():
            return
        key = str(mon_path)
        with self._watcher_lock:
            if self._alerted_paths.get(key) == stage:
                return
            self._alerted_paths[key] = stage
        MessageHelper().put(message, title="目录监控")

    @staticmethod
    def __poller_alert(storage: str, message: str) -> None:
        """
        远程轮询监控告警回调，复用消息渠道推送。
        :param storage: 存储名称
        :param message: 告警内容
        """
        logger.warn(f"[{storage}] {message}")
        MessageHelper().put(message, title="目录监控")

    def __clear_alert(self, mon_path: Path, message: str) -> None:
        """
        清除目录监控异常告警状态，并在此前告警过时推送恢复消息。
        :param mon_path: 监控目录
        :param message: 恢复内容
        """
        if not self.__accepting_work():
            return
        key = str(mon_path)
        with self._watcher_lock:
            if key not in self._alerted_paths:
                return
            self._alerted_paths.pop(key, None)
        logger.info(message)
        MessageHelper().put(message, title="目录监控")

    def polling_observer(self, storage: str, mon_paths: List[Path]) -> None:
        """
        轮询监控：执行一轮快照并按结果动态调整监控间隔。
        """
        if not self.__accepting_work():
            return
        file_count = self._poller.poll(storage=storage, mon_paths=mon_paths)
        if not self.__accepting_work() or file_count is None or not self._scheduler:
            return
        # 动态调整监控间隔
        new_interval = SnapshotStore.adjust_interval(file_count)
        try:
            current_job = self._scheduler.get_job(f"monitor_{storage}")
            if current_job and current_job.trigger.interval.total_seconds() / 60 != new_interval:
                self._scheduler.modify_job(
                    f"monitor_{storage}",
                    trigger='interval',
                    minutes=new_interval
                )
                logger.info(f"{storage} 监控间隔已调整为 {new_interval} 分钟")
        except Exception as e:
            logger.error(f"调整监控间隔失败: {storage} - {e}")

    def event_handler(
            self, event: Any, text: str, event_path: str,
            file_size: Optional[float] = None
    ) -> None:
        """
        处理文件变化。
        :param event: 事件
        :param text: 事件描述
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        """
        if not self.__accepting_work() or event.is_directory:
            return
        if not self._dispatcher.is_transfer_candidate_path(Path(event_path)):
            return
        file_modify_time = None
        try:
            file_modify_time = Path(event_path).stat().st_mtime
        except OSError as err:
            logger.debug(f"读取目录监控文件修改时间失败: {event_path} - {err}")
        if not self.__accepting_work():
            return
        # 整理文件
        handle_kwargs = {
            "storage": "local",
            "event_path": Path(event_path),
            "file_size": file_size,
        }
        if file_modify_time is not None:
            handle_kwargs["file_modify_time"] = file_modify_time
        self._dispatcher.handle_file(**handle_kwargs)

    def event_unreadable(self, event_path: Path) -> None:
        """
        处理读取失败的监控事件，登记待重试。
        :param event_path: 事件文件路径
        """
        if not self.__accepting_work():
            return
        event_path = Path(event_path)
        if not self._dispatcher.is_transfer_candidate_path(event_path):
            return
        self._dispatcher.register_unreadable(storage="local", event_path=event_path)

    def stop(self, timeout: float = RELOAD_STOP_TIMEOUT) -> bool:
        """在共享预算内临时停止全部监控 owner，供配置热重载使用。"""
        return self.__stop_with_budget(timeout=timeout, close=False)

    def close(self, timeout: float = LIFECYCLE_CLOSE_TIMEOUT) -> bool:
        """永久封口当前 lifespan，并在共享预算内等待全部监控 owner。"""
        return self.__stop_with_budget(timeout=timeout, close=True)

    def __stop_with_budget(self, timeout: float, close: bool) -> bool:
        """从等待生命周期锁开始计算一次停止操作的完整预算。"""
        timeout = max(0.0, timeout)
        deadline = time.monotonic() + timeout
        # 先封住工作入口再等待锁。配置重载若正阻塞在 FUSE 上，解冻后也只能清理，
        # 不能在外层生命周期已经超时返回后继续创建 watcher 或派发整理任务。
        self._work_stop_event.set()
        if close:
            self._shutdown_event.set()
        if not self._lifecycle_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
        ):
            logger.error("目录监控未在 %.1f 秒内取得停机所有权", timeout)
            return False
        try:
            return self.__stop_owned(deadline=deadline, close=close)
        finally:
            self._lifecycle_lock.release()

    def __stop_owned(self, deadline: float, close: bool) -> bool:
        """
        在已取得生命周期锁时等待全部 owner，共享调用方给出的绝对截止时间。

        超时只报告未收敛并保留所有活句柄；只有 watcher、补偿扫描、恢复动作及
        scheduler shutdown 都到达终态后，才清空这一代的内存状态。
        """
        self._work_stop_event.set()
        if close:
            self._closed = True
            self._shutdown_event.set()
        self._recovery.request_stop()
        self.__request_scheduler_stop()

        with self._watcher_lock:
            watchers = tuple(self._watchers) + tuple(self._retired_watchers)
        if watchers:
            logger.info("正在停止本地目录监控服务...")
        for watcher in watchers:
            try:
                watcher.stop()
            except Exception as err:
                logger.error(f"请求停止目录监控服务出现了错误：{err}")

        recovery_converged = self._recovery.close(deadline=deadline)
        current_thread = threading.current_thread()
        # 恢复线程可能在封口后才从 FUSE 调用返回，并登记一个已经请求停止的新
        # watcher；RecoveryExecutor 收敛后重新取快照，确保它也使用剩余预算 join。
        with self._watcher_lock:
            registered_watchers = tuple(self._watchers) + tuple(self._retired_watchers)
        for watcher in registered_watchers:
            try:
                watcher.stop()
                watcher.join(timeout=max(0.0, deadline - time.monotonic()))
            except Exception as err:
                logger.error(f"等待目录监控服务停止出现了错误：{err}")

        with self._owner_lock:
            compensation_threads = tuple(self._compensation_threads.values())
            scheduler_thread = self._scheduler_shutdown_thread
        for thread in compensation_threads:
            if thread is not current_thread and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if (
                scheduler_thread is not None
                and scheduler_thread is not current_thread
                and scheduler_thread.is_alive()
        ):
            scheduler_thread.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._watcher_lock:
            alive_watchers = tuple(
                watcher
                for watcher in (*self._watchers, *self._retired_watchers)
                if watcher.is_alive()
            )
        with self._owner_lock:
            alive_compensations = tuple(
                thread for thread in self._compensation_threads.values()
                if thread.is_alive()
            )
            scheduler_thread = self._scheduler_shutdown_thread
            scheduler_converged = (
                self._scheduler is None
                or (
                    scheduler_thread is not None
                    and not scheduler_thread.is_alive()
                    and self._scheduler_shutdown_succeeded
                )
            )
        alive_recoveries = self._recovery.running_threads()
        if (
                alive_watchers
                or alive_compensations
                or alive_recoveries
                or not recovery_converged
                or not scheduler_converged
        ):
            logger.error(
                "目录监控 owner 未在截止时间内收敛：watcher=%d，补偿=%d，恢复=%d，scheduler=%s",
                len(alive_watchers),
                len(alive_compensations),
                len(alive_recoveries),
                "未收敛" if not scheduler_converged else "已收敛",
            )
            return False

        # 仅在所有 owner 真实终止后清理业务状态，确保失败重试仍能找到原句柄。
        self._dispatcher.clear_pending()
        with self._watcher_lock:
            self._watchers = []
            self._retired_watchers = []
            self._pending_locals = []
            self._alerted_paths = {}
            self._restart_marks = {}
            self._stable_cycles = {}
            self._isolated = {}
            self._pending_rebuild = {}
        self._recovery.clear()
        with self._owner_lock:
            self._compensation_threads.clear()
            self._scheduler = None
            self._scheduler_shutdown_thread = None
            self._scheduler_shutdown_succeeded = False
        if watchers:
            logger.info("本地目录监控服务已停止")
        return True

    def __request_scheduler_stop(self) -> None:
        """创建并登记唯一 scheduler shutdown 线程，避免阻塞生命周期调用线程。"""
        with self._owner_lock:
            running = self._scheduler_shutdown_thread
            if running is not None and running.is_alive():
                return
            if running is not None and self._scheduler_shutdown_succeeded:
                return
            scheduler = self._scheduler
            if scheduler is None:
                return
            self._scheduler_shutdown_succeeded = False
            thread = threading.Thread(
                target=self.__shutdown_scheduler,
                args=(scheduler,),
                name="MoviePilot-MonitorSchedulerShutdown",
                daemon=True,
            )
            self._scheduler_shutdown_thread = thread
            try:
                thread.start()
            except Exception as err:
                self._scheduler_shutdown_thread = None
                logger.error(f"启动定时监控停止线程失败：{err}")

    def __shutdown_scheduler(self, scheduler: BackgroundScheduler) -> None:
        """停止 scheduler 及其在途 job，并把真实终态写回 owner 注册表。"""
        succeeded = False
        try:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown(wait=True)
            succeeded = True
            logger.info("定时监控服务已停止")
        except Exception as err:
            logger.error(f"停止定时服务出现了错误：{err}")
        finally:
            with self._owner_lock:
                if self._scheduler is scheduler:
                    self._scheduler_shutdown_succeeded = succeeded
        # 缓存与快照存储是共享后端的代理，生命周期由应用全局管理，这里不再关闭
