import threading
import tracemalloc
from collections import defaultdict
import os
from datetime import datetime
from typing import Optional

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.utils.singleton import Singleton
from app.log import logger


class DiagMemoryHelper(metaclass=Singleton):
    """
    内存诊断工具类，使用 trace malloc 进行微观内存分配分析。
    只在调试模式下开启，有性能开销
    """

    def __init__(self):
        # 核心状态锁
        self._lock = threading.Lock()

        # 工作线程，负责生成内存报告
        self._report_thread: Optional[threading.Thread] = None
        self._report_stop_event = threading.Event()

        # 状态标志
        self._is_monitoring = False

        # 用于累计报告
        self._baseline_snapshot = None
        # 用于增量报告
        self._last_snapshot = None

        # 用于优雅处理的锁，确保报告生成不被打断
        self._report_generation_lock = threading.Lock()

        # 用于记录增量报告的 logger
        self._increment_logger = logger.get_logger("diag_memory_increment")
        # 用于记录累计报告的 logger
        self._cumulative_logger = logger.get_logger("diag_memory_cumulative")

    def start_monitoring(self):
        """
        程序启动时，是否开启监控。
        """
        if settings.MEMORY_DIAGNOSTICS:
            return self._start()

    def stop_monitoring(self):
        """
        应用退出时进行优雅的资源清理。
        """
        # 直接调用内部的停止方法即可
        return self._stop()

    def _start(self):
        """
        启动内存分配追踪
        """
        with self._lock:
            if self._is_monitoring:
                return

            pid = os.getpid()
            tracemalloc.start(settings.MEMORY_DIAGNOSTICS_STACK_DEPTH)
            initial_snapshot = tracemalloc.take_snapshot()
            self._baseline_snapshot = initial_snapshot
            self._last_snapshot = initial_snapshot

            if self._report_stop_event:
                self._report_stop_event.clear()

            self._report_thread = threading.Thread(
                target=self._periodic_report,
                daemon=True,
                name=f"DiagMemoryMonitor-{pid}"
            )
            self._report_thread.start()
            self._is_monitoring = True
            logger.warning(f"!!! [PID:{pid}] 内存诊断模式已开启 !!!")

    def _stop(self):
        """
        停止内存分配追踪和报告线程。
        """
        with self._lock:
            if not self._is_monitoring:
                logger.info("内存诊断未在运行中，无需停止。")
                return

            pid = os.getpid()
            logger.warning(f"!!! [PID:{pid}] 开始停止内存诊断 !!!")

            # 发送停止信号给报告线程
            self._report_stop_event.set()

            # 等待报告线程优雅退出
            thread_to_join = self._report_thread
            if thread_to_join and thread_to_join.is_alive():
                logger.info("等待报告线程完成当前任务...")
                thread_to_join.join(timeout=settings.MEMORY_REPORTING_INTERVAL + 5)

            # 停止 tracemalloc 并清理资源
            if tracemalloc.is_tracing():
                tracemalloc.stop()

            # 重置状态
            self._baseline_snapshot = None
            self._last_snapshot = None
            self._report_thread = None
            self._is_monitoring = False
            logger.info("内存诊断已完全停止，并成功清理相关资源。")

    def _periodic_report(self):
        """
        定时汇报
        """
        pid = os.getpid()
        while not self._report_stop_event.wait(timeout=settings.MEMORY_REPORTING_INTERVAL):
            with self._report_generation_lock:
                if self._report_stop_event.is_set():
                    break
                try:
                    if not tracemalloc.is_tracing():
                        break

                    current_snapshot = tracemalloc.take_snapshot()

                    # 生成并记录增量报告
                    incremental_stats = current_snapshot.compare_to(self._last_snapshot, 'traceback')
                    self._log_report_stats(
                        stats=incremental_stats,
                        report_type="增量报告",
                        pid=pid,
                        interval=settings.MEMORY_REPORTING_INTERVAL
                    )
                    # 更新 last_snapshot 以便下次生成增量报告
                    self._last_snapshot = current_snapshot

                    cumulative_stats = current_snapshot.compare_to(self._baseline_snapshot, 'traceback')
                    self._log_report_stats(
                        stats=cumulative_stats,
                        report_type="累计报告",
                        pid=pid
                    )

                except Exception as e:
                    logger.error(f"[DiagMemory][PID:{pid}] 报告生成失败: {e}", exc_info=True)

    def _log_report_stats(self, stats, report_type, pid, interval=None):
        """
        格式化并记录内存统计报告。
        """
        # 调整logger
        report_logger = self._increment_logger if report_type == "增量报告" else self._cumulative_logger

        if not stats:
            if report_type == "增量报告":
                report_logger.info(f"[DiagMemory][PID:{pid}] 在过去 {interval}s 内，内存分配稳定。")
            return

        report_by_module = defaultdict(lambda: {'total_growth': 0, 'stats': []})
        for stat in stats:
            if not stat.traceback:
                continue
            source_file = stat.traceback[0].filename
            report_by_module[source_file]['total_growth'] += stat.size_diff
            report_by_module[source_file]['stats'].append(stat)

        if not report_by_module:
            return

        sorted_modules = sorted(report_by_module.items(), key=lambda item: item[1]['total_growth'], reverse=True)

        report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title_interval = f"[最近 {interval}s]" if interval else ""
        report_logger.info(f"--- [{report_type}][PID:{pid}][{report_time}]{title_interval} ---")

        for module_path, data in sorted_modules[:5]:
            total_growth_kb = data['total_growth'] / 1024
            if total_growth_kb < 1:
                continue
            report_logger.info(f"[PID:{pid}] 模块: {module_path} | 总增长: +{total_growth_kb:.2f} KB")
            for stat in sorted(data['stats'], key=lambda s: s.size_diff, reverse=True)[:3]:
                report_logger.warning(f"--> {stat}")
        report_logger.info(f"--- [报告结束][PID:{pid}] ---")

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件
        :param event: 事件对象
        """
        if not event:
            return
        event_data: schemas.ConfigChangeEventData = event.event_data
        if event_data.key not in ['MEMORY_DIAGNOSTICS', 'MEMORY_DIAGNOSTICS_STACK_DEPTH',
                                  'MEMORY_DIAGNOSTICS_STACK_DEPTH'
                                  ]:
            return

        with self._report_generation_lock:
            self._stop()
            if settings.MEMORY_DIAGNOSTICS:
                self._start()
