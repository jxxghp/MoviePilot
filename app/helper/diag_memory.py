import threading
import time
import tracemalloc
from collections import defaultdict
import os

from app.core.config import settings
from app.utils.singleton import Singleton
from app.log import logger

# 将报告记录在独立的 diag_memory.log 文件中
logger = logger.get_logger("diag_memory")


class DiagMemoryHelper(metaclass=Singleton):
    """
    内存诊断工具类，使用 trace malloc 进行微观内存分配分析。
    只在调试模式下开启，有性能开销
    """

    def __init__(self):
        self._monitor_started = False
        self._lock = threading.Lock()
        self._baseline_snapshot = None

    def start_monitoring(self):
        """
        为当前进程启动内存分配追踪。
        """
        with self._lock:
            if self._monitor_started:
                return

            pid = os.getpid()
            logger.warning(f"!!! [PID:{pid}] 内存诊断模式已开启 !!!")

            tracemalloc.start(settings.MEMORY_DIAGNOSTICS_STACK_DEPTH)
            self._baseline_snapshot = tracemalloc.take_snapshot()

            monitor_thread = threading.Thread(
                target=self._periodic_report,
                daemon=True,
                name=f"DiagMemoryMonitor-{pid}"
            )
            monitor_thread.start()
            self._monitor_started = True

    def _periodic_report(self):
        """
        定时汇报
        """
        pid = os.getpid()
        while True:
            time.sleep(settings.MEMORY_REPORTING_INTERVAL)

            try:
                current_snapshot = tracemalloc.take_snapshot()
                top_stats = current_snapshot.compare_to(self._baseline_snapshot, 'traceback')

                if not top_stats:
                    logger.info(f"[DiagMemory][PID:{pid}] 内存分配稳定，未检测到显著增长。")
                    continue

                report_by_module = defaultdict(lambda: {'total_growth': 0, 'stats': []})

                for stat in top_stats:
                    # 检查 traceback 是否为空
                    if not stat.traceback:
                        continue
                    source_file = stat.traceback[0].filename
                    report_by_module[source_file]['total_growth'] += stat.size_diff
                    report_by_module[source_file]['stats'].append(stat)

                if not report_by_module:
                    continue

                sorted_modules = sorted(report_by_module.items(), key=lambda item: item[1]['total_growth'],
                                        reverse=True)

                logger.warning(f"--- [内存诊断报告][PID:{pid}] ---")
                for module_path, data in sorted_modules[:5]:
                    total_growth_kb = data['total_growth'] / 1024
                    # 忽略过小幅度的增长
                    if total_growth_kb < 1:
                        continue
                    logger.warning(f"  [PID:{pid}] 模块: {module_path} | 总增长: {total_growth_kb:.2f} KB")
                    for stat in sorted(data['stats'], key=lambda s: s.size_diff, reverse=True)[:3]:
                        logger.warning(f" 增长位置 -> {stat}")
                logger.warning(f"--- [报告结束][PID:{pid}] ---")
            except Exception as e:
                logger.error(f"[DiagMemory][PID:{pid}] 报告生成失败: {e}", exc_info=True)
