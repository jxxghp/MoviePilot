import traceback
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.poller import RemotePoller
from app.monitor.snapshot import SnapshotStore
from app.monitor.syslimits import decide_monitor_mode, get_system_optimization_tips
from app.monitor.watcher import LocalDirectoryWatcher
from app.schemas.types import SystemConfigKey
from app.utils.mixins import ConfigReloadMixin
from app.utils.singleton import SingletonClass
from app.utils.system import SystemUtils


class Monitor(ConfigReloadMixin, metaclass=SingletonClass):
    """
    目录监控门面，单例模式：装配本地/远程监控、维护生命周期与健康检查。
    """
    CONFIG_WATCH = {SystemConfigKey.Directories.value}
    # 目录监控健康检查间隔（秒）
    WATCHDOG_INTERVAL = 60
    # 连续多少个健康检查周期无新增重启后才宣告恢复，避免反复崩溃时告警刷屏
    RECOVERY_STABLE_CYCLES = 5

    def __init__(self):
        super().__init__()
        # 本地目录监控服务
        self._watchers = []
        # 本地目录监控列表读写锁
        self._watcher_lock = Lock()
        # 启动失败待重试的本地监控配置
        self._pending_locals: List[Dict[str, Any]] = []
        # 已告警的监控目录，避免重复推送
        self._alerted_paths: set = set()
        # 各监控目录已告警过的自动重启次数
        self._restart_marks: Dict[str, int] = {}
        # 各监控目录连续稳定的健康检查周期数
        self._stable_cycles: Dict[str, int] = {}
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
        self.init()

    def on_config_changed(self):
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

    def init(self):
        """
        启动监控
        """
        # 停止现有任务
        self.stop()

        # 读取目录配置
        monitor_dirs = DirectoryHelper().get_download_dirs()
        if not monitor_dirs:
            logger.info("未找到任何目录监控配置")
            return

        messagehelper = MessageHelper()

        # 先筛出有效的监控配置，再按下载目录去重，避免非监控配置顶掉监控配置
        valid_dirs = []
        for mon_dir in monitor_dirs:
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
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        mon_storages: Dict[str, List[Path]] = {}
        # 本地监控启动结果计数，用于输出真实的启动总结
        local_started = 0
        local_failed = 0
        for mon_dir in monitor_dirs:
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
            # 远程目录监控 - 使用智能间隔
            # 先尝试加载已有快照获取文件数量
            snapshot_data = self._store.load(storage)
            file_count = snapshot_data.get('file_count', 0) if snapshot_data else 0
            interval = SnapshotStore.adjust_interval(file_count)
            for path in paths:
                logger.info(f"正在启动远程目录监控: {path} [{storage}]")
            logger.info("*** 重要提示：远程目录监控只处理新增和修改的文件，不会处理监控启动前已存在的文件 ***")
            logger.info(f"预估文件数量: {file_count}, 监控间隔: {interval}分钟")

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

    def __start_local_monitor(self, mon_path: Path, monitor_mode: str) -> bool:
        """
        启动单个本地目录监控，失败时登记待重试。
        :param mon_path: 监控目录
        :param monitor_mode: 配置的监控模式
        :return: 是否启动成功
        """
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
                poll_delay_ms = LocalDirectoryWatcher.POLL_DELAY_NETWORK_MS
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

    def __handle_start_failure(self, mon_path: Path, monitor_mode: str, err: Exception):
        """
        处理本地目录监控启动失败，登记待重试并按需告警。
        :param mon_path: 监控目录
        :param monitor_mode: 配置的监控模式
        :param err: 启动异常
        """
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

    def watchdog(self):
        """
        目录监控健康检查：重建崩溃或静默失效的监控线程，并重试启动失败的监控目录。
        """
        try:
            self.__check_watchers()
            self.__retry_pending_locals()
            self._dispatcher.retry_pending()
        except Exception as e:
            logger.error(f"目录监控健康检查出现错误：{e}\n{traceback.format_exc()}")

    def __check_watchers(self):
        """
        检查本地目录监控线程状态，异常时重建。
        """
        with self._watcher_lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            key = str(watcher.watch_path)
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
                else:
                    # 稳定满恢复窗口才宣告恢复，避免反复崩溃时告警/恢复消息来回刷屏
                    self._stable_cycles[key] = self._stable_cycles.get(key, 0) + 1
                    if self._stable_cycles[key] >= self.RECOVERY_STABLE_CYCLES:
                        self.__clear_alert(watcher.watch_path, f"目录监控已恢复正常: {watcher.watch_path}")
                continue
            logger.error(f"目录监控异常: {watcher.watch_path} - {reason}，正在重建监控线程 ...")
            self.__send_alert(watcher.watch_path,
                              f"目录监控异常: {watcher.watch_path}\n原因: {reason}\n正在自动重建监控")
            self.__rebuild_watcher(watcher)

    def __rebuild_watcher(self, watcher: LocalDirectoryWatcher):
        """
        重建一个本地目录监控线程。
        :param watcher: 需要重建的监控
        """
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
            with self._watcher_lock:
                self._watchers = [item for item in self._watchers if item is not watcher]
                if all(pending["mon_path"] != watcher.watch_path for pending in self._pending_locals):
                    self._pending_locals.append({
                        "mon_path": watcher.watch_path,
                        # 重建沿用原监控模式，force_polling 为 True 即兼容模式
                        "monitor_mode": "compatibility" if watcher.force_polling else "fast"
                    })
            return
        with self._watcher_lock:
            self._watchers = [new_watcher if item is watcher else item for item in self._watchers]
        # 新监控的重启计数从零开始，同步重置告警基准
        self._restart_marks.pop(str(watcher.watch_path), None)
        self._stable_cycles.pop(str(watcher.watch_path), None)
        logger.info(f"✓ 目录监控已重建: {watcher.watch_path}")
        self.__clear_alert(watcher.watch_path, f"目录监控已自动恢复: {watcher.watch_path}")

    def __retry_pending_locals(self):
        """
        重试启动失败的本地目录监控，给网络存储/FUSE 挂载留出就绪时间。
        """
        with self._watcher_lock:
            pending = list(self._pending_locals)
        for item in pending:
            # 失败次数越多重试间隔越长（按健康检查周期数退避），长时间故障时不刷屏
            if item.get("skip_cycles", 0) > 0:
                item["skip_cycles"] -= 1
                continue
            logger.info(f"重试启动本地目录监控: {item['mon_path']}")
            if not self.__start_local_monitor(mon_path=item["mon_path"], monitor_mode=item["monitor_mode"]):
                item["attempts"] = item.get("attempts", 0) + 1
                item["skip_cycles"] = min(item["attempts"], 10)

    def __send_alert(self, mon_path: Path, message: str):
        """
        推送目录监控异常告警，同一目录仅在状态变化时推送一次。
        :param mon_path: 监控目录
        :param message: 告警内容
        """
        key = str(mon_path)
        with self._watcher_lock:
            if key in self._alerted_paths:
                return
            self._alerted_paths.add(key)
        MessageHelper().put(message, title="目录监控")

    @staticmethod
    def __poller_alert(storage: str, message: str):
        """
        远程轮询监控告警回调，复用消息渠道推送。
        :param storage: 存储名称
        :param message: 告警内容
        """
        logger.warn(f"[{storage}] {message}")
        MessageHelper().put(message, title="目录监控")

    def __clear_alert(self, mon_path: Path, message: str):
        """
        清除目录监控异常告警状态，并在此前告警过时推送恢复消息。
        :param mon_path: 监控目录
        :param message: 恢复内容
        """
        key = str(mon_path)
        with self._watcher_lock:
            if key not in self._alerted_paths:
                return
            self._alerted_paths.discard(key)
        logger.info(message)
        MessageHelper().put(message, title="目录监控")

    def polling_observer(self, storage: str, mon_paths: List[Path]):
        """
        轮询监控：执行一轮快照并按结果动态调整监控间隔。
        """
        file_count = self._poller.poll(storage=storage, mon_paths=mon_paths)
        if file_count is None or not self._scheduler:
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

    def event_handler(self, event, text: str, event_path: str, file_size: float = None):
        """
        处理文件变化。
        :param event: 事件
        :param text: 事件描述
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        """
        if event.is_directory:
            return
        if not self._dispatcher.is_transfer_candidate_path(Path(event_path)):
            return
        # 整理文件
        self._dispatcher.handle_file(storage="local", event_path=Path(event_path), file_size=file_size)

    def stop(self):
        """
        退出监控
        """
        # 先停定时服务，避免健康检查在停止过程中重建监控线程
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                try:
                    self._scheduler.shutdown()
                    logger.info("定时监控服务已停止")
                except Exception as e:
                    logger.error(f"停止定时服务出现了错误：{e}")
            self._scheduler = None
        with self._watcher_lock:
            watchers = self._watchers
            self._watchers = []
            self._pending_locals = []
            self._alerted_paths = set()
            self._restart_marks = {}
            self._stable_cycles = {}
        if watchers:
            logger.info("正在停止本地目录监控服务...")
            for watcher in watchers:
                try:
                    watcher.stop()
                    watcher.join(timeout=5)
                    if watcher.is_alive():
                        logger.warning(f"本地目录监控线程在5秒内未能停止: {watcher.watch_path}")
                    else:
                        logger.debug(f"已停止本地目录监控服务: {watcher.watch_path}")
                except Exception as e:
                    logger.error(f"停止目录监控服务出现了错误：{e}")
            logger.info("本地目录监控服务已停止")
        # 缓存与快照存储是共享后端的代理，生命周期由应用全局管理，这里不再关闭
